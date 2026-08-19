"""★ A3 超预算自动降级（W19 Day5 欠账落地）：token 计数器 + 降级不拒绝
★ W27 Day3 A8：预算计数迁 Redis（INCRBYFLOAT 跨实例共享）+ Redis 挂本地近似

语义（W10 P1 "超预算自动降级（非拒绝）"）：
- 每个会话一个 token 计数器（真实 usage 累计，LLMProvider 返回的 usage 或估算值）
- 超预算 → 降级：intent 改规则匹配、报表改模板生成、生成改 mock——**不是拒绝**
- 降级触发要在日志/span 打标记（"降级有据可查"）

★ A8 Redis 化（多实例共享预算水位）：
- `add_usage` 优先 Redis `INCRBYFLOAT cost:{session_id}:input_tokens|output_tokens`（原子累计）
- `status` 读 Redis 权威值（跨实例准确）；Redis 挂 → 本地近似 + DEGRADED 日志 + metrics
- `BudgetExceeded` 供写路径硬限制（raise_on_exceed=True）；读路径默认降级不抛（A3 语义）
- ADR 注释：预算是软限制，Redis 抖动不阻塞业务 → fail-open 合理（降级到本地近似）

注意：cost_usage 记录的是 LLM 调用；本计数器由编排层（graph/respond）手动累加
（从 generate/generate_json 返回值或 provider 回调中取 token 数）。
"""
import threading
import time

from app.shared.obs import logger as obs_logger

_log = obs_logger.get_logger("reliability.cost_budget")


class BudgetExceeded(Exception):
    """预算超限（★ W27 D3 A8）。写路径硬限制用；读路径默认降级不拒绝（A3）。

    调用方在需要"宁可拒绝也不超支"的场景捕获；普通降级语义走 SessionBudget.degraded。
    """


def estimate_tokens_from_messages(messages: list[dict]) -> int:
    """无 usage 时的 token 估算（中文约 1 字 ≈ 1 token 的粗略口径，仅预算用）。"""
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
    return total


class SessionBudget:
    """会话级预算：累计成本(¥) + 超预算降级标记（★ A8：Redis 权威 + 本地降级近似）。

    price: (input_元_每百万token, output_元_每百万token)
    """

    def __init__(self, budget_yuan: float = 0.5,
                 price_input: float = 2.0, price_output: float = 8.0,
                 session_id: str = "", redis_client=None):
        self.budget_yuan = budget_yuan
        self.price_input = price_input
        self.price_output = price_output
        self.session_id = session_id
        self._redis = redis_client          # None = 不启用 Redis 计数（纯本地近似）
        self._lock = threading.Lock()
        self._redis_down_logged = False
        # 本地字段：Redis 可用时同步为 Redis 值；Redis 挂时作为降级近似
        self.total_input_tokens: float = 0.0
        self.total_output_tokens: float = 0.0
        self.cost_yuan: float = 0.0
        self.degraded = False            # 超预算降级标记（span 里打标记用）
        self.degraded_at: str | None = None

    @property
    def _in_key(self) -> str:
        return f"cost:{self.session_id}:input_tokens"

    @property
    def _out_key(self) -> str:
        return f"cost:{self.session_id}:output_tokens"

    def add_usage(self, prompt_tokens: int, completion_tokens: int,
                  raise_on_exceed: bool = False) -> None:
        """累加一次 LLM 调用的 usage（真实 token 数），并更新成本与降级状态。

        ★ A8：优先 Redis INCRBYFLOAT（跨实例共享预算水位）；Redis 挂 → 本地近似。
        raise_on_exceed=True 时超限抛 BudgetExceeded（写路径硬限制，读路径默认不抛）。
        """
        if self._redis is not None and self._redis.available:
            try:
                spent_in = self._redis.incrbyfloat(self._in_key, float(prompt_tokens))
                spent_out = self._redis.incrbyfloat(self._out_key, float(completion_tokens))
                if spent_in is not None and spent_out is not None:
                    self.total_input_tokens = spent_in
                    self.total_output_tokens = spent_out
                    self.cost_yuan = (spent_in * self.price_input
                                      + spent_out * self.price_output) / 1_000_000
                    self._check_budget(raise_on_exceed)
                    return
            except Exception:
                pass                    # Redis 操作异常 → 落本地近似
            self._log_redis_down()      # available True 但操作抛错 → Redis 刚挂
        elif self._redis is not None:
            self._log_redis_down()      # Redis 不可用 → 降级本地（首次记日志 + metrics）
        # Redis 挂 / 未启用 → 本地近似（A8 ADR：预算是软限制，fail-open 合理）
        with self._lock:
            self.total_input_tokens += prompt_tokens
            self.total_output_tokens += completion_tokens
            self.cost_yuan = (self.total_input_tokens * self.price_input
                              + self.total_output_tokens * self.price_output) / 1_000_000
        self._check_budget(raise_on_exceed)

    def _check_budget(self, raise_on_exceed: bool) -> None:
        """超限判定：置粘滞降级标记（日志）+ 可选抛 BudgetExceeded。"""
        if not self.degraded and self.cost_yuan >= self.budget_yuan:
            self.degraded = True
            self.degraded_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
            print(f"  [BUDGET] 会话成本 ¥{self.cost_yuan:.4f} ≥ 预算 ¥{self.budget_yuan}"
                  f" → 触发超预算降级（不拒绝，质量降但可用）")
        if raise_on_exceed and self.cost_yuan >= self.budget_yuan:
            raise BudgetExceeded(
                f"会话预算超限：¥{self.cost_yuan:.4f} ≥ 预算 ¥{self.budget_yuan}")

    def _log_redis_down(self) -> None:
        """Redis 不可用降级本地（A8）：只记一次日志 + metrics 计数，避免刷屏。"""
        if self._redis_down_logged:
            return
        self._redis_down_logged = True
        from app.shared.obs.metrics import inc_budget_redis_down
        inc_budget_redis_down()
        obs_logger.log_event(
            _log, "budget_redis_down", level="warning",
            session_id=self.session_id, backend="local",
            note="预算计数降级本地近似（软限制 fail-open，A8）")

    def is_over_budget(self) -> bool:
        """是否超预算（当前成本 ≥ 预算；跨实例时读 Redis 权威值）。"""
        return self.status()["cost_yuan"] >= self.budget_yuan

    def status(self) -> dict:
        """预算状态。★ A8：Redis 可用时从 Redis 读权威计数（跨实例准确）；
        Redis 挂 → 本地近似值。"""
        if self._redis is not None and self._redis.available:
            try:
                spent_in = self._redis.get(self._in_key)
                spent_out = self._redis.get(self._out_key)
                if spent_in is not None and spent_out is not None:
                    self.total_input_tokens = float(spent_in)
                    self.total_output_tokens = float(spent_out)
                    self.cost_yuan = (self.total_input_tokens * self.price_input
                                      + self.total_output_tokens * self.price_output) / 1_000_000
            except Exception:
                pass
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cost_yuan": round(self.cost_yuan, 6),
            "budget_yuan": self.budget_yuan,
            "degraded": self.degraded,
            "degraded_at": self.degraded_at,
        }


# 会话预算注册表：{session_id: SessionBudget}（进程内；多实例共享走 Redis 计数）
_budgets: dict[str, SessionBudget] = {}
_budgets_lock = threading.Lock()


def get_session_budget(session_id: str, budget_yuan: float | None = None,
                       price_input: float | None = None,
                       price_output: float | None = None,
                       redis_client=None) -> SessionBudget:
    """取会话预算（懒创建）。参数只在创建时生效。

    redis_client：默认全局单例（生产启用 Redis 计数）；测试可注入 fake。
    注册表已存在时复用实例（测试换 client 先 reset_budgets()）。
    """
    global _budgets
    with _budgets_lock:
        if session_id not in _budgets:
            from app.shared import config
            from app.shared.reliability.redis_client import get_redis_client
            _budgets[session_id] = SessionBudget(
                budget_yuan=budget_yuan if budget_yuan is not None else config.SESSION_BUDGET_YUAN,
                price_input=price_input if price_input is not None else config.COST_PRICE_INPUT,
                price_output=price_output if price_output is not None else config.COST_PRICE_OUTPUT,
                session_id=session_id,
                redis_client=redis_client if redis_client is not None else get_redis_client(),
            )
        return _budgets[session_id]


def reset_budgets():
    """测试用：清空预算注册表。"""
    global _budgets
    with _budgets_lock:
        _budgets = {}
