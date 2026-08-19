"""scm-copilot-client：SCM Copilot 平台官方 Python SDK（薄封装 httpx，零重依赖）。

三接口（对应 W25 手册 Day5 验收：pip install 后 10 行跑通）：
- `chat_stream(prompt, ...)`: SSE 流式问答（kb/ops 双域，事件迭代器）
- `nl2sql(question, as_dataframe=False)`: 自然语言查数据 → 表格 + SQL
- `approvals.list_pending() / decide(id, action)`: 审批（机器身份接入 HITL）

认证：`api_key="sk-..."`（机器身份，推荐）或 `token="<JWT>"`（用户身份）二选一；
两者都不传时只能访问放行端点。httpx.Client 线程安全，多线程可共用。

十行示例：
    from scm_client import ScmCopilot
    client = ScmCopilot("http://localhost:8000", api_key="sk-xxx")
    for event in client.chat_stream("供应商准入需要哪些资质？"):
        print(event.delta, end="", flush=True)
    result = client.nl2sql("近30天延迟发货 TOP5 供应商", as_dataframe=True)
    print(result.sql)
    pending = client.approvals.list_pending()
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterator

import httpx

from scm_client.approvals import Approvals
from scm_client.chat import parse_sse_events
from scm_client.data import build_dataframe
from scm_client.errors import ErrorCode, ScmAuthError, ScmError, ScmQuotaError, ScmServerError
from scm_client.models import ApprovalItem, ChatEvent, Nl2SqlResult

__all__ = [
    "ScmCopilot",
    "ChatEvent",
    "Nl2SqlResult",
    "ApprovalItem",
    "ScmError",
    "ScmAuthError",
    "ScmQuotaError",
    "ScmServerError",
    "ErrorCode",
]

__version__ = "0.2.0"


class ScmCopilot:
    """平台客户端（同步 httpx；SSE 用 stream 迭代器消费）。"""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
        verify: bool | str = True,
        auto_retry: bool = True,
        max_retries: int = 2,
    ):
        """初始化客户端。

        ★ W27 Day4：`auto_retry=True`（默认）时对幂等请求自动退避重试：
        - 429（ScmQuotaError）→ 尊重服务端 `Retry-After`（≤30s 才重试，避免放大雪崩）
        - 5xx / 超时 → 指数退避 + 抖动（base 2^n + jitter，上限 8s）
        非幂等写操作（如 approvals.decide 已带 approval_id 幂等键，可安全重试；
        若调用方自定义非幂等请求，应传 `auto_retry=False` 关闭）。
        """
        self.base_url = base_url.rstrip("/")
        self.auto_retry = auto_retry
        self.max_retries = max(0, int(max_retries))
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        elif token:
            headers["Authorization"] = f"Bearer {token}"
        if client is not None:
            # 自定义 client（如 MockTransport/连接池复用）：补认证头，不接管其余管理
            client.headers.update(headers)
            self._client = client
        else:
            self._client = httpx.Client(
                base_url=self.base_url, headers=headers, timeout=timeout,
                verify=verify,  # ★ W25 Day6：mkcert 本地 TLS 平台 / 自签 CA 内网
            )
        self.approvals = Approvals(self)

    # ---------------- 内部请求封装 ----------------

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """统一请求入口：非 2xx → 抛 `ScmError`（据 Err 契约解析 code/message/trace_id）。

        ★ W27 Day4 自动退避（`auto_retry`，默认开）：
        - 429：有 `Retry-After` 且 ≤30s → sleep 后重试；无/超 30s 立即抛（不猜服务端）
        - 5xx / httpx 超时：指数退避 + 抖动（`min(2**attempt + random, 8)`）
        其他 4xx（认证/校验）不重试——重试也不会变好。
        """
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code >= 400:
                    raise ScmError.from_response(resp)
                return resp
            except ScmQuotaError as e:  # 429：尊重服务端节流
                if not self.auto_retry or attempt >= self.max_retries \
                        or not e.retry_after or e.retry_after > 30:
                    raise
                time.sleep(e.retry_after)
            except (ScmServerError, httpx.TimeoutException):  # 5xx/超时：指数退避+抖动
                if not self.auto_retry or attempt >= self.max_retries:
                    raise
                time.sleep(min(2 ** attempt + random.random(), 8))
        raise RuntimeError("unreachable")  # pragma: no cover

    # ---------------- 三接口 ----------------

    def chat_stream(
        self,
        prompt: str,
        session_id: str | None = None,
        domain: str = "kb",
    ) -> Iterator[ChatEvent]:
        """SSE 流式问答：kb（知识问答）或 ops（业务操作），事件迭代器。

        每个 `ChatEvent` 带 `type` 与原始 `data`；`event.delta` 便捷取 message
        增量（打字机输出）。流结束后迭代器自然终止；HTTP 错误在流起始抛 `ScmError`。
        """
        if domain not in ("kb", "ops"):
            raise ValueError(f"domain 必须是 'kb' 或 'ops'，收到 {domain!r}")
        body: dict[str, str] = {"message": prompt}
        if session_id:
            body["session_id"] = session_id
        path = f"/api/v1/{domain}/chat"
        with self._client.stream("POST", path, json=body) as resp:
            if resp.status_code >= 400:
                raise ScmError.from_response(resp)
            yield from parse_sse_events(resp.iter_lines())

    def nl2sql(
        self,
        question: str,
        as_dataframe: bool = False,
        session_id: str | None = None,
        today: str | None = None,
    ) -> Nl2SqlResult:
        """自然语言查数据：返回表格 + SQL（可审计可纠错）+ 洞察摘要。

        `as_dataframe=True` 时附带 `result.df`（pandas.DataFrame，需要
        `pip install scm-copilot-client[dataframe]`）。`result.sql` 始终透出。
        """
        body: dict[str, object] = {"question": question}
        if session_id:
            body["session_id"] = session_id
        if today:
            body["today"] = today
        payload = self._request("POST", "/api/v1/data/query", json=body).json()
        result = Nl2SqlResult.from_payload(payload)
        if as_dataframe:
            result.df = build_dataframe(payload)
        return result

    # ---------------- 生命周期 ----------------

    def close(self) -> None:
        """关闭底层 httpx.Client（释放连接池）。"""
        self._client.close()

    def __enter__(self) -> ScmCopilot:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.close()
