"""★ 多轮会话上下文与指代消解（W24 Day5）——"那华南呢？" → "上个月华南的延迟订单有多少"。

对应《W24学习执行手册》Day5 下午 +《03》1.4 节：
- 会话内保存上一轮 {question, sql, tables}；新问题做指代消解（LLM 一次调用）：
  补全省份/时间/实体省略，**消解后的问题再进生成链路**（SQL 生成感知不到多轮——
  两个关注点分开，各自评测）；
- mock 双路径（手册坑"mock 测链路、real 测效果"）：
  - provider=mock → 确定性规则消解（_mock_resolve，支持区域/时间/状态替换与补插），测链路；
  - provider=real → `build_resolve_messages` + 模型池（真实消解能力，效果只算 real）；
- 无状态化说明：当前为进程内 LRU 缓存（TTL 30min，多实例各持一份）；
  正式多实例会话持久化归 W25（MySQL conversations 表，已有 touch_conversation 通路）。

用法（router 侧）：
    ctx = get_session(session_id)            # 无则创建
    resolved = await ctx.resolve(question, today)
    state = await data_graph.ainvoke({...})  # 用 resolved 入图
    if 查询成功: ctx.record(resolved, sql, tables)

对外接口：
    get_session(session_id) -> SessionContext     # 全局注册表（惰性建 + TTL 清理）
    clear_sessions() -> None                      # 测试隔离
    SessionContext.record(question, sql, tables)
    SessionContext.resolve(question, today) -> str
    build_resolve_messages(prev_question, prev_sql, question) -> list[dict]
"""

from __future__ import annotations

import re
import time

from app.domains.data.prompts import DATA_BASE_DATE
from app.shared.llm import get_provider

# ==================== mock 规则消解（确定性，测链路） ====================

_TIMES = ("近7天", "近30天", "上个月", "近90天")
_REGIONS = ("华东", "华北", "华南", "西南")
_STATUSES = ("已支付", "已发货", "已完成", "已取消", "草稿")

# 各实体类别的匹配模式（替换 prev 中最后一个同类实体）
_TIME_RE = r"近\d+天|上个月|近90天"
_REGION_RE = "华东|华北|华南|西南"
_STATUS_RE = "已支付|已发货|已完成|已取消|草稿"


def _replace_last(text: str, pattern: str, repl: str) -> str | None:
    """把 text 中最后一个匹配 pattern 的片段替换为 repl；无匹配返回 None。"""
    last: re.Match[str] | None = None
    for _last in re.finditer(pattern, text):
        last = _last
    if last is None:
        return None
    return text[: last.start()] + repl + text[last.end():]


def _mock_resolve(prev: str, question: str) -> str:
    """规则式指代消解（mock 链路用）：识别"那X呢 / 只看X呢 / X呢"模式并补全。"""
    # 注意：中文句末问号是全角"？"（U+FF1F），半角"?"也兼容
    m = re.match(r"^(?:那|那么)?(.+?)呢[？?]?$", question.strip())
    if not m:
        return question.strip()
    x = m.group(1).strip()
    x = re.sub(r"^(?:只看|只算|换成|改为|都改成)?", "", x).strip()

    # 1) 时间替换/补插：prev 有同类时间 → 替换；否则前置
    if x in _TIMES:
        out = _replace_last(prev, _TIME_RE, x)
        return out if out is not None else f"{x}{prev}"
    # 2) 区域替换/补插：优先换区域词；其次换"各区域"；否则前置
    if x in _REGIONS:
        out = _replace_last(prev, _REGION_RE, x)
        if out is not None:
            return out
        out = _replace_last(prev, "各区域", f"{x}区域")
        return out if out is not None else f"{x}区域{prev}"
    # 3) "那各区域呢？" → 把区域限定换成"各区域"
    if x == "各区域":
        out = _replace_last(prev, "(?:华东|华北|华南|西南)区域", "各区域")
        if out is not None:
            return out
        out = _replace_last(prev, _REGION_RE, "各区域")
        return out if out is not None else f"各区域{prev}"
    # 4) 状态替换/补插："已完成的"→"已完成"
    if x in _STATUSES or x.endswith("的"):
        sx = x.rstrip("的")
        out = _replace_last(prev, _STATUS_RE, sx)
        if out is not None:
            return out
        # 无状态 → 在第一个"订单"前补插（"订单数量"也命中同一位置）
        idx = prev.find("订单")
        if idx != -1:
            return prev[:idx] + f"{sx}的" + prev[idx:]
        return f"{sx}{prev}"
    return question.strip()


# ==================== real 消解 prompt ====================


def _clean_resolved(raw: str) -> str:
    """清洗 LLM 消解输出：去引号/代码块围栏，取首行。"""
    text = (raw or "").strip()
    text = re.sub(r"^```(?:text|plain)?\s*", "", text).strip()
    text = re.sub(r"```$", "", text).strip()
    text = text.splitlines()[0].strip() if text else ""
    return text.strip("\"'「」『』")


def build_resolve_messages(
    prev_question: str, prev_sql: str, question: str
) -> list[dict[str, str]]:
    """指代消解 prompt：一次 LLM 调用，把当前问题补全为独立完整问题。

    手册 Day5 坑：指代消解是**单独一次 LLM 调用**，别塞进 SQL 生成 prompt——
    两个关注点（补全 + 生成 SQL）混在一起两头都差。
    """
    system = (
        "你是中文对话指代消解助手。用户在向一个供应链数据查询助手连续提问。\n"
        "把用户【当前问题】补全为一条独立、完整的自然语言问题：\n"
        "把省略的区域（华东/华北/华南/西南）、时间窗（近7天/近30天/上个月）、"
        "订单状态（已支付/已发货/已完成/已取消）等条件从上一轮问题中补全进来。\n"
        "硬性规则：\n"
        "1. 不得改变业务含义，不得编造上一轮未出现的信息；\n"
        "2. 上一轮 SQL 只作为上下文参考，不要在结果里输出 SQL；\n"
        "3. 只输出补全后的一条完整问题，不要任何解释。"
    )
    user = (
        f"上一轮问题：{prev_question}\n"
        f"上一轮查询 SQL：{prev_sql}\n\n"
        f"当前问题：{question}\n\n"
        "补全后的完整问题："
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


# ==================== 会话上下文 ====================

DEFAULT_TTL_SECONDS = 30 * 60  # 30min 过期（进程内缓存；W25 迁 MySQL 持久化）
DEFAULT_MAX_TURNS = 4


class SessionContext:
    """一次多轮会话的上下文：保存最近几轮 {question, sql, tables} 供指代消解。"""

    def __init__(
        self,
        session_id: str,
        max_turns: int = DEFAULT_MAX_TURNS,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        self.session_id = session_id
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.updated_at = time.monotonic()
        self._turns: list[dict[str, object]] = []

    # ---- 状态 ----

    def record(self, question: str, sql: str, tables: list[str] | None = None) -> None:
        """记录一轮已消解的完整问题（作为下轮消解的上下文来源）。"""
        self._turns.append({"question": question, "sql": sql, "tables": tables or []})
        if len(self._turns) > self.max_turns:
            self._turns.pop(0)
        self.updated_at = time.monotonic()

    def recent(self) -> dict[str, object] | None:
        """最近一轮上下文（None = 首轮）。"""
        return self._turns[-1] if self._turns else None

    def is_expired(self) -> bool:
        return time.monotonic() - self.updated_at > self.ttl_seconds

    # ---- 指代消解 ----

    async def resolve(self, question: str, today: str | None = None) -> str:
        """消解当前问题为独立完整问题（首次/无上下文 → 原样返回）。

        - provider=mock：规则消解（确定性，测链路）；
        - provider=real：`build_resolve_messages` → 模型池生成 → 清洗。
        """
        prev = self.recent()
        if prev is None:
            return question.strip()

        provider = get_provider()
        if provider.name == "mock":
            return _mock_resolve(str(prev["question"]), question)

        today = today or DATA_BASE_DATE.isoformat()  # noqa: F841  # 保留 today 供后续 prompt 版本使用
        messages = build_resolve_messages(
            str(prev["question"]), str(prev["sql"]), question
        )
        raw = await provider.generate(messages, max_tokens=256, temperature=0.0)
        return _clean_resolved(raw)


# ==================== 全局会话注册表（进程内 LRU + TTL） ====================

_MAX_SESSIONS = 1024
_SESSIONS: dict[str, SessionContext] = {}


def get_session(session_id: str) -> SessionContext:
    """按 session_id 取会话上下文（无则创建；过期/超量触发清理）。"""
    if len(_SESSIONS) >= _MAX_SESSIONS:
        # 简单防膨胀：清掉过期项，仍超限则整体重置（进程内缓存，重启即失）
        _SESSIONS.clear()
    ctx = _SESSIONS.get(session_id)
    if ctx is None or ctx.is_expired():
        ctx = SessionContext(session_id)
        _SESSIONS[session_id] = ctx
    return ctx


def clear_sessions() -> None:
    """清空会话注册表（测试隔离）。"""
    _SESSIONS.clear()
