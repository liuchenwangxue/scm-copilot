"""RealLLMProvider：真实 LLM 接入（★ W18 中段落地，Day3 实现）。

通道：OpenAI 兼容协议（httpx 直调，不引大框架）。
实测通道（2026-08-12 验证通过）：
    base_url = https://dashscope.aliyuncs.com/compatible-mode/v1
    model    = 由 config.LLM_MODEL / 模型池指定（deepseek 系列已弃用，不再使用）
Key 只从环境变量/.env 读取（config.LLM_API_KEY），绝不入库。

三接口 + 生产三件套（W10）：
- 超时：30s（流式 60s）
- 重试：指数退避 3 次 + jitter（429/超时/5xx 可重试；400/401 不重试）
- 错误分类：限流/超时/参数错误分别处理（HTTPError 带状态码）

降级链（W18 手册 Day3 要求，可开关，默认开）：
    real 异常 → 记录 ERROR → 按错误分类重试 → 仍失败 → 返回 mock 结果并打 WARNING 标记
降级开关：LLM_DEGRADE_TO_MOCK=1（默认开）；=0 时异常直接上抛（评测需显式感知失败）。

成本记录（W10 成本层接口预留）：
- 解析 usage.prompt_tokens / completion_tokens（含 reasoning_tokens，推理模型特有）
- 写日志 JSON line（cost_usage.jsonl），供 Day5 成本实测校准
- LangFuse generation span（LANGFUSE_ENABLED=1 时，W9 复用；观测失败不阻塞业务）

生成提示语（Day4 会搬到 prompts.py，此处先内联）：
- generate_json 要求回答必须引用 doc_id（citations），且只依据提供的上下文
- 解析容错：截取首个 '{' 到末个 '}' 再 json.loads
"""
import asyncio
import json
import random
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from app.shared import config
from app.shared.llm.base import LLMProvider
from app.shared.llm.mock_provider import MockLLMProvider

# 超时 / 重试参数（W10 三件套）
TIMEOUT_S = float(config.LLM_TIMEOUT if hasattr(config, "LLM_TIMEOUT") else 30)
STREAM_TIMEOUT_S = 60.0
MAX_RETRIES = 2        # 单模型内重试次数（省 token：额度耗尽直接切模型，不多重试）
BASE_DELAY = 0.5

_JSON_RE = re.compile(r"\{.*\}", re.S)

# ★ 模型池：单模型额度耗尽时自动切换（按顺序循环使用）
# W24 Day5 更新（2026-08-18）：根据 DashScope 控制台截图全量配置——已剔除 glm-5.2
# （免费额度早已耗尽，每次先探它会白白浪费一次 HTTP 调用）；
# 当前活跃模型 kimi-k2.7-code 置首位 + qwen3.7-max-2026-06-08（剩余 99%）次之；
# 剩余按字母/版本近顺序：plus-2026-05-26 / max-2026-05-20 / max-2026-05-17 /
# max-preview / plus / max / qwen3.8-2.4t-a95b / deepseek-v4-pro-0813。
# ★ 持久化最近使用的模型到 reports/llm_model_state.json：下次进程启动直接接续，
# 跳过已耗尽模型，避免每个新进程都要从 glm-5.2 探一遍再切。
DEFAULT_MODEL_POOL = [
    "kimi-k2.7-code",                  # 当前活跃（实测可用）
    "qwen3.7-max-2026-06-08",          # 剩余 99.4%，最新快照版本
    "qwen3.7-plus-2026-05-26",
    "qwen3.7-max-2026-05-20",
    "qwen3.7-max-2026-05-17",
    "qwen3.7-max-preview",
    "qwen3.7-plus",
    "qwen3.7-max",
    "qwen3.8-2.4t-a95b",
    "deepseek-v4-pro-0813",
]

# 额度耗尽/权限类错误关键词（命中即切下一个模型）
_QUOTA_KEYWORDS = (
    "quota", "balance", "insufficient", "exhausted", "access denied",
    "rate limit", "free tier", "额度", "余额", "限流", "用量", "403",
    "max_tokens", "maximum context", "token limit", "额度不足", "欠费",
)

# 全局模型切换状态（多 provider 实例共享——并发评测时统一切换）
_model_pool_state: dict[str, Any] = {"idx": 0, "models": list(DEFAULT_MODEL_POOL)}


def _state_file() -> Path:
    """当前活跃模型持久化文件（★ Day5：避免每进程先探 glm-5.2）。

    与 cost_usage.jsonl 同目录，便于管理；本文件不在 .gitignore 中——但只存模型名，
    不含 Key/敏感信息（Key 仅在 .env 中，.gitignore 已保护）。
    """
    return config.REPORTS_DIR / "llm_model_state.json"


def _load_active_model() -> str | None:
    """读取上次成功调用的模型名（None = 首次启动，按池顺序）。"""
    p = _state_file()
    if not p.exists():
        return None
    try:
        import json as _json

        name = str(_json.loads(p.read_text(encoding="utf-8")).get("model") or "").strip()
        return name or None
    except Exception:  # noqa: BLE001  # 文件脏不影响业务
        return None


def _save_active_model(model: str) -> None:
    """记录当前成功调用的模型名（写盘最佳努力，失败不影响业务）。"""
    try:
        import json as _json

        p = _state_file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            _json.dumps(
                {"model": model, "updated_at": time.time()},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        pass


def _pool_models() -> list[str]:
    """当前模型池（可被环境变量 LLM_MODEL_POOL 覆盖，逗号分隔）。"""
    import os
    v = os.getenv("LLM_MODEL_POOL")
    if v and v.strip():
        models = [m.strip() for m in v.split(",") if m.strip()]
        if models:
            return models
    pool = _model_pool_state["models"]
    return list(pool) if isinstance(pool, list) else list(DEFAULT_MODEL_POOL)


def reorder_pool_by_active(pool: list[str]) -> tuple[list[str], int]:
    """把"上次成功调用的模型"挪到池首位（★ Day5：避免每进程先探已耗尽模型）。

    返回 (新池顺序, 该模型在新池中的下标)；持久化缺失/不匹配则原样返回。
    """
    active = _load_active_model()
    if not active or active not in pool:
        return list(pool), 0
    idx = pool.index(active)
    return pool[idx:] + pool[:idx], idx


def _is_quota_error(exc: BaseException) -> bool:
    """是否为"模型额度耗尽"类错误（需要切模型，而非普通重试）。"""
    if isinstance(exc, _ProviderError):
        if exc.status == 429:
            return True
        if exc.status in (400, 403):
            return _has_quota_kw(exc.args[0] if exc.args else "")
    text = str(exc)
    return _has_quota_kw(text)


def _has_quota_kw(text: str) -> bool:
    t = text.lower()
    return any(k in t for k in _QUOTA_KEYWORDS)


class _ProviderError(RuntimeError):
    """带 HTTP 状态码的 Provider 错误，用于错误分类。"""

    def __init__(self, status: int | None, message: str):
        super().__init__(message)
        self.status = status


def _retryable(exc: BaseException) -> bool:
    """哪些错误值得重试（W10 is_retryable 移植）：
    429 限流 / 超时 / 5xx 可重试；400/401/403 参数与权限不可重试。"""
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, asyncio.TimeoutError)):
        return True
    if isinstance(exc, _ProviderError):
        if exc.status is None:
            return True
        return exc.status == 429 or exc.status >= 500
    text = str(exc)
    return any(k in text for k in ("timeout", "超时", "Connection", "连接", "503", "502", "504"))


def _parse_usage(payload: dict) -> dict:
    """解析 usage，兼容推理模型（reasoning_tokens 计入 completion）。"""
    u = payload.get("usage") or {}
    return {
        "prompt_tokens": int(u.get("prompt_tokens", 0)),
        "completion_tokens": int(u.get("completion_tokens", 0)),
        "total_tokens": int(u.get("total_tokens", 0)),
        "reasoning_tokens": int((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)),
    }


def _log_cost(usage: dict, model: str, tag: str) -> None:
    """成本/usage 记录：追加 JSON line（Day5 成本实测的数据源）。

    ★ W26 Day1：同步记录 Prometheus Counter（scm_llm_tokens_total /
    scm_llm_cost_yuan_total，label=model）——Grafana "成本看板" 面板
    token 用量按模型 / 单轮成本 / 日预算水位的数据源。
    """
    try:
        cost_yuan = _estimate_cost_yuan(usage)
        _inc_cost_metrics(model, usage, cost_yuan)
    except Exception:
        pass  # 指标旁路失败不影响业务
    try:
        f = config.REPORTS_DIR / "cost_usage.jsonl"
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "model": model, "tag": tag,
                "prompt_tokens": usage["prompt_tokens"],
                "completion_tokens": usage["completion_tokens"],
                "reasoning_tokens": usage.get("reasoning_tokens", 0),
                "total_tokens": usage["total_tokens"],
            }, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 记录失败不影响业务


def _estimate_cost_yuan(usage: dict) -> float:
    """按单价估算本轮成本（¥/百万 token，W24 成本口径）。"""
    try:
        prompt = float(usage.get("prompt_tokens", 0) or 0)
        completion = float(usage.get("completion_tokens", 0) or 0)
        input_price = float(config.COST_PRICE_INPUT)
        output_price = float(config.COST_PRICE_OUTPUT)
        return round((prompt * input_price + completion * output_price) / 1_000_000, 6)
    except Exception:
        return 0.0


def _inc_cost_metrics(model: str, usage: dict, cost_yuan: float) -> None:
    """把 token/成本写入 Prometheus（fail-open，观测旁路）。"""
    try:
        from app.shared.obs.metrics import inc_llm_usage
        inc_llm_usage(
            model,
            prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage.get("completion_tokens", 0) or 0),
            cost_yuan=cost_yuan,
        )
    except Exception:
        pass


class _Observability:
    """LangFuse 观测（旁路，fail-open）：LLM_ENABLE_LANGFUSE=1 时挂 generation span。"""

    def __init__(self):
        self.enabled = config.LLM_ENABLE_LANGFUSE if hasattr(config, "LLM_ENABLE_LANGFUSE") else False
        self._lf = None

    def _get(self):
        if not self.enabled:
            return None
        if self._lf is None:
            try:
                from langfuse import Langfuse
                self._lf = Langfuse(
                    public_key=config.LANGFUSE_PUBLIC_KEY,
                    secret_key=config.LANGFUSE_SECRET_KEY,
                    host=config.LANGFUSE_HOST,
                    debug=False,
                )
            except Exception:
                self.enabled = False
        return self._lf

    def generation(self, name: str, model: str, messages: list[dict], output: str | None,
                   usage: dict | None, metadata: dict | None = None):
        lf = self._get()
        if lf is None:
            return
        try:
            gen = lf.generation(
                name=name, model=model,
                input={"messages": messages[-2:]},
                output=output or "", metadata=metadata or {},
            )
            if usage:
                md = dict(metadata or {})
                md["reasoning_tokens"] = usage.get("reasoning_tokens", 0)
                gen.update(
                    usage={
                        "input": usage["prompt_tokens"],
                        "output": usage["completion_tokens"],
                        "total": usage["total_tokens"],
                    },
                    metadata=md,
                )
            gen.end()
            lf.flush()
        except Exception:
            pass  # 观测失败不影响业务


_obs = _Observability()


class RealLLMProvider(LLMProvider):
    name = "real"

    def __init__(self, degrade_to_mock: bool | None = None, model_override: str | None = None,
                 models: list[str] | None = None):
        if not config.LLM_API_KEY:
            raise RuntimeError("LLM_API_KEY 未配置：请设置环境变量或在 .env 中填写（Key 严禁入库）")
        if not config.LLM_BASE_URL:
            raise RuntimeError("LLM_BASE_URL 未配置")
        # 模型池：models > model_override > 环境变量池 > config.LLM_MODEL
        if models:
            self.models = models
        elif model_override:
            self.models = [model_override]
        else:
            # ★ Day5：把"上次成功调用的模型"挪到池首位——避免每进程先探已耗尽模型
            self.models, _start = reorder_pool_by_active(_pool_models())
            _model_pool_state["idx"] = _start
        if not self.models:
            raise RuntimeError("LLM_MODEL 未配置")
        # 当前使用的模型 = 全局池指针指向的模型（多实例共享切换状态）
        self._model_idx = int(_model_pool_state["idx"]) % len(self.models)
        self.model = self.models[self._model_idx]
        # 降级开关：默认开；优先级 参数 > config.LLM_DEGRADE_TO_MOCK（.env/环境变量）> 默认开
        if degrade_to_mock is not None:
            self.degrade_to_mock = degrade_to_mock
        else:
            v = config.LLM_DEGRADE_TO_MOCK
            self.degrade_to_mock = True if v == "" else v.strip().lower() in ("1", "true", "yes", "on")
        self._mock: MockLLMProvider | None = None
        self._client: httpx.AsyncClient | None = None  # 复用 HTTP client（省连接握手，加速）
        self._client_loop: asyncio.AbstractEventLoop | None = None  # 跨 loop 复用会报错

    # ---------- 内部：HTTP 调用 ----------

    def _get_client(self) -> httpx.AsyncClient:
        """懒创建 + 复用 HTTP client；★跨 event loop 修复（W21 Day1）：
        httpx.AsyncClient 绑定创建时的 loop，不同 loop 复用会报
        "Event loop is closed"。检测当前 loop ≠ 创建 loop 时重建。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if self._client is None or (loop is not None and self._client_loop is not loop):
            self._client = httpx.AsyncClient(
                base_url=config.LLM_BASE_URL,
                headers={"Authorization": f"Bearer {config.LLM_API_KEY}"},
                timeout=httpx.Timeout(TIMEOUT_S, read=STREAM_TIMEOUT_S),
            )
            self._client_loop = loop
        return self._client

    def _switch_model(self) -> str:
        """切换到下一个模型（全局共享，所有 provider 实例同步）。"""
        idx = int(_model_pool_state["idx"])
        _model_pool_state["idx"] = (idx + 1) % len(_pool_models())
        self._model_idx = int(_model_pool_state["idx"]) % len(self.models)
        self.model = self.models[self._model_idx]
        print(f"\n  [MODEL-SWITCH] 模型额度耗尽，切换 -> {self.model}")
        return self.model

    @staticmethod
    def _build_messages(messages: list[dict], kw: dict) -> list[dict]:
        """拼装 messages：优先用 kw 的 system_prompt_override（Day4 prompts.py 传入），
        否则 fallback 到内置 RAG 系统提示（带 retrieval_context 时）。"""
        ctx = kw.get("retrieval_context")
        if not ctx:
            return messages
        system = kw.get("system_prompt_override")
        if system is None:
            # 兜底：内置拼装（一般不会走到这里，上层通常传 system_prompt_override）
            from app.shared.llm.prompts import build_rag_context, build_system_prompt
            system = build_system_prompt(build_rag_context(ctx))
        return [{"role": "system", "content": system}, *messages]

    @staticmethod
    def _extract_json(text: str) -> dict:
        """解析容错：截取首个 { 到末个 } 再 json.loads（推理模型可能带思考前后缀）。"""
        m = _JSON_RE.search(text or "")
        if not m:
            raise ValueError(f"响应中未找到 JSON 对象: {text[:120]}")
        return json.loads(m.group(0))

    # ---------- 三接口 ----------

    async def generate(self, messages: list[dict], **kw) -> str:
        msgs = self._build_messages(messages, kw)
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": kw.get("temperature", 0.2),
            "max_tokens": kw.get("max_tokens", 2048),
            # ★ 关闭推理（省 token + 提速 + 防 JSON 截断）：推理模型 reasoning 极耗 token，
            # 且挤占 max_tokens 导致正文截断。DashScope 兼容 thinking 参数。
            "thinking": {"type": "disabled"},
        }
        try:
            t_start = time.time()
            data, usage = await self._post_chat(payload, "generate")
            latency = round(time.time() - t_start, 3)
            content = data["choices"][0]["message"].get("content") or ""
            _log_cost(usage, self.model, "generate")
            _obs.generation("llm.real_generate", self.model, msgs, content, usage,
                            {"latency_s": latency})
            return content
        except Exception as e:
            return await self._degrade_or_raise(e, "generate", msgs, kw)

    def stream(self, messages: list[dict], **kw) -> AsyncIterator[str]:
        """流式：SSE 逐块 yield delta.content。推理模型先吐 reasoning_content 再 content，
        reasoning 块直接跳过（只对外吐正文字符，Day5 成本对比以 usage 为准）。"""
        async def gen():
            msgs = self._build_messages(messages, kw)
            payload = {
                "model": self.model,
                "messages": msgs,
                "temperature": kw.get("temperature", 0.2),
                "max_tokens": kw.get("max_tokens", 2048),
                "stream": True,
                "thinking": {"type": "disabled"},
            }
            full = []
            usage = {}
            try:
                client = self._get_client()
                async with client.stream("POST", "/chat/completions", json=payload) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread()).decode("utf-8", "replace")[:200]
                        raise _ProviderError(resp.status_code, f"HTTP {resp.status_code}: {body}")
                    async for line in resp.aiter_lines():
                        line = (line or "").strip()
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("usage"):
                            usage = _parse_usage(chunk)
                        delta = (chunk.get("choices") or [{}])[0].get("delta", {})
                        content = delta.get("content")
                        if content:
                            full.append(content)
                            yield content
                _log_cost(usage, self.model, "stream")
                _obs.generation("llm.real_stream", self.model, msgs, "".join(full), usage)
            except Exception as e:
                # 流式降级：错误已产生部分输出时丢弃，返回 mock 文本并标记
                if self.degrade_to_mock:
                    fallback = await self._mock_async_answer(msgs, kw)
                    yield f"\n[WARNING] real 流式失败({_err_summary(e)})，已降级 mock 回答\n{fallback}"
                else:
                    raise
        return gen()

    async def generate_json(self, messages: list[dict], schema: dict, **kw) -> dict:
        msgs = self._build_messages(messages, kw)
        payload = {
            "model": self.model,
            "messages": msgs,
            "temperature": kw.get("temperature", 0.0),
            "max_tokens": kw.get("max_tokens", 2048),
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        # JSON 解析失败（输出被截断/不完整）→ 重试 1 次仍失败则上抛原始异常
        # （让调用方决定：校验器场景由 _llm_check except 兜底返回 PASS；主生成场景走降级链）
        for _try in range(2):
            try:
                t_start = time.time()
                data, usage = await self._post_chat(payload, "generate_json")
                latency = round(time.time() - t_start, 3)
                content = data["choices"][0]["message"].get("content") or ""
                _log_cost(usage, self.model, "generate_json")
                parsed = self._extract_json(content)
                _obs.generation("llm.real_generate_json", self.model, msgs, content, usage,
                                {"citations": parsed.get("citations"), "latency_s": latency})
                return parsed
            except (ValueError, json.JSONDecodeError) as e:
                if _try == 0:
                    print(f"  [REAL] generate_json JSON 解析失败，重试一次: {str(e)[:80]}")
                    continue
                raise
            except Exception as e:
                return await self._degrade_or_raise(e, "generate_json", msgs, kw)
        # 循环内要么 return 要么 raise，走到这里是防御性兜底（mypy 需要显式出口）
        raise RuntimeError("generate_json 未返回结果")

    # ---------- 重试 / 降级 ----------

    async def _post_chat(self, payload: dict, tag: str) -> tuple[dict, dict]:
        """带重试 + 模型切换的 POST。返回 (json_body, usage)。

        优先级（省 token 设计）：
        1. 额度耗尽错误（429/403/400+配额关键词）→ 立即切换模型重试（不浪费重试次数）
        2. 瞬时错误（超时/5xx）→ 单模型内指数退避重试 MAX_RETRIES 次
        3. 池内模型全试过仍失败 → 上抛（走降级链）

        ★ 并发安全（Day5 修复活锁）：不再每轮外层循环重读共享 self.model——
        本请求取【本地模型顺序快照】确定性逐个尝试；每命中一次额度耗尽，全局指针 +1
        （下次请求从下一个模型开始）。旧版在并发下多请求同步切换，指针反复回到已耗尽
        的 qwen3.8-max，导致 glm/kimi/qwen3.7 没被真正尝试（用户反馈"只有 qwen 在跑"
        的根因）。修复后每个请求都会真正尝试到池内每个模型。
        """
        client = self._get_client()
        last_exc = None
        pool = _pool_models() or self.models
        # 从全局指针处开始，本地顺序快照（并发时各请求起点错开，但都会尝试到每个模型）
        start = int(_model_pool_state["idx"]) % len(pool)
        order = pool[start:] + pool[:start]
        tried: set[str] = set()

        # ★ W22 Day3：OTEL span 包住 LLM 调用（Traces 支柱；未启用 → noop，fail-open）
        span = None
        span_ctx = None
        span_token = None
        try:
            from app.shared.obs import otel as _otel
            tracer = _otel.get_tracer()
            if tracer is not None:
                from opentelemetry import context as _ocontext
                from opentelemetry import trace as _otrace
                span = tracer.start_span(f"llm.{tag}", attributes={"llm.tag": tag})
                span_ctx = _otrace.set_span_in_context(span)
                span_token = _ocontext.attach(span_ctx)
        except Exception:
            span = None
            span_ctx = None
            span_token = None

        try:
            for model in order:
                if model in tried:
                    continue
                tried.add(model)

                # 内层：单模型内指数退避重试
                for attempt in range(MAX_RETRIES + 1):
                    payload["model"] = model
                    try:
                        t0 = time.time()
                        resp = await client.post("/chat/completions", json=payload)
                        if resp.status_code != 200:
                            body = resp.text[:300]
                            raise _ProviderError(resp.status_code, f"HTTP {resp.status_code}: {body}")
                        data = resp.json()
                        usage = _parse_usage(data)
                        # 成功后 self.model 指向实际成功模型（保持多请求认知一致）
                        self.model = model
                        _save_active_model(model)  # ★ Day5：持久化，下次进程直接接续
                        if span is not None:
                            span.set_attributes({
                                "llm.model": model,
                                "llm.latency_ms": round((time.time() - t0) * 1000, 1),
                                "llm.prompt_tokens": usage["prompt_tokens"],
                                "llm.completion_tokens": usage["completion_tokens"],
                                "llm.total_tokens": usage["total_tokens"],
                            })
                        return data, usage
                    except Exception as e:
                        last_exc = e
                        if span is not None:
                            span.record_exception(e)
                            span.set_attribute("llm.error", f"{type(e).__name__}: {str(e)[:80]}")
                        # 额度耗尽 → 切换模型（立即切，不浪费重试）
                        if _is_quota_error(e):
                            if len(tried) >= len(order):
                                break
                            print(f"  [REAL-QUOTA] {tag} {model} 额度耗尽({_err_summary(e)[:60]})")
                            self._switch_model()
                            await asyncio.sleep(BASE_DELAY)
                            break  # 跳出内层，进入下一个模型
                        # 瞬时错误 → 单模型内重试
                        if attempt >= MAX_RETRIES:
                            break
                        if not _retryable(e):
                            raise
                        delay = BASE_DELAY * (2 ** attempt) + random.uniform(0, BASE_DELAY)
                        print(f"  [REAL-RETRY] {tag} {model} 第{attempt + 1}次失败 ({_err_summary(e)})，"
                              f"{delay:.1f}s 后重试")
                        await asyncio.sleep(delay)

            raise RuntimeError(f"{tag} 所有模型({len(order)})均失败: {last_exc}")
        finally:
            if span is not None and span_token is not None:
                try:
                    from opentelemetry import context as _ocontext
                    _ocontext.detach(span_token)
                    span.end()
                except Exception:
                    pass

    async def _mock_async_answer(self, msgs: list[dict], kw: dict) -> str:
        if self._mock is None:
            self._mock = MockLLMProvider()
        return await self._mock.generate(msgs, **kw)

    async def _degrade_or_raise(self, exc: Exception, tag: str, msgs: list[dict], kw: dict) -> Any:
        """降级链：real 异常 → 打 WARNING → 返回 mock 结果（可开关）。

        返回类型与调用接口对齐：
        - tag=generate_json：mock 的 generate_json dict（{"answer","citations"}）+
          `degraded=True` 标记（明确告知降级）；
        - tag=generate/stream：`[WARNING]` 前缀 mock 文本。

        ★ W26 Day2 演练四修复：原实现一律返回 str，generate_json 调用方拿到
        str 会破坏 JSON 契约（下游 json 解析失败）——降级也必须守住接口契约。"""
        if _is_quota_error(exc):
            raise exc
        if self.degrade_to_mock:
            print(f"  [REAL-DEGRADE] {tag} 失败 ({_err_summary(exc)})，降级 mock（LLM_DEGRADE_TO_MOCK=1）")
            if self._mock is None:
                self._mock = MockLLMProvider()
            if tag == "generate_json":
                js = await self._mock.generate_json(msgs, {}, **kw)
                if isinstance(js, dict):
                    js["degraded"] = True
                    js["degrade_reason"] = _err_summary(exc)
                return js
            mock_ans = await self._mock.generate(msgs, **kw)
            return f"[WARNING] real 失败降级: {_err_summary(exc)}\n{mock_ans}"
        raise exc


def _err_summary(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:120]}"
