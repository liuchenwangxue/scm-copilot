"""★ A3 超预算自动降级（W19 Day5 欠账落地）：token 计数器 + 降级不拒绝

语义（W10 P1 "超预算自动降级（非拒绝）"）：
- 每个会话一个 token 计数器（真实 usage 累计，LLMProvider 返回的 usage 或估算值）
- 超预算 → 降级：intent 改规则匹配、报表改模板生成、生成改 mock——**不是拒绝**
- 降级触发要在日志/span 打标记（"降级有据可查"）

注意：cost_usage 记录的是 LLM 调用；本计数器由编排层（graph/respond）手动累加
（从 generate/generate_json 返回值或 provider 回调中取 token 数）。
"""
import threading
import time


def estimate_tokens_from_messages(messages: list[dict]) -> int:
    """无 usage 时的 token 估算（中文约 1 字 ≈ 1 token 的粗略口径，仅预算用）。"""
    total = 0
    for m in messages:
        total += len(str(m.get("content", "")))
    return total


class SessionBudget:
    """会话级预算：累计成本(¥) + 超预算降级标记。

    price: (input_元_每百万token, output_元_每百万token)
    """

    def __init__(self, budget_yuan: float = 0.5,
                 price_input: float = 2.0, price_output: float = 8.0):
        self.budget_yuan = budget_yuan
        self.price_input = price_input
        self.price_output = price_output
        self._lock = threading.Lock()
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.cost_yuan = 0.0
        self.degraded = False            # 超预算降级标记（span 里打标记用）
        self.degraded_at: str | None = None

    def add_usage(self, prompt_tokens: int, completion_tokens: int) -> None:
        """累加一次 LLM 调用的 usage（真实 token 数），并更新成本与降级状态。"""
        with self._lock:
            self.total_input_tokens += prompt_tokens
            self.total_output_tokens += completion_tokens
            self.cost_yuan = (self.total_input_tokens * self.price_input
                              + self.total_output_tokens * self.price_output) / 1_000_000
            if not self.degraded and self.cost_yuan >= self.budget_yuan:
                self.degraded = True
                self.degraded_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
                print(f"  [BUDGET] 会话成本 ¥{self.cost_yuan:.4f} ≥ 预算 ¥{self.budget_yuan}"
                      f" → 触发超预算降级（不拒绝，质量降但可用）")

    def is_over_budget(self) -> bool:
        return self.degraded

    def status(self) -> dict:
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cost_yuan": round(self.cost_yuan, 6),
            "budget_yuan": self.budget_yuan,
            "degraded": self.degraded,
            "degraded_at": self.degraded_at,
        }


# 会话预算注册表：{session_id: SessionBudget}（进程内；多实例生产用 Redis）
_budgets: dict[str, SessionBudget] = {}
_budgets_lock = threading.Lock()


def get_session_budget(session_id: str, budget_yuan: float | None = None,
                       price_input: float | None = None,
                       price_output: float | None = None) -> SessionBudget:
    """取会话预算（懒创建）。参数只在创建时生效。"""
    global _budgets
    with _budgets_lock:
        if session_id not in _budgets:
            from app.shared import config
            _budgets[session_id] = SessionBudget(
                budget_yuan=budget_yuan if budget_yuan is not None else config.SESSION_BUDGET_YUAN,
                price_input=price_input if price_input is not None else config.COST_PRICE_INPUT,
                price_output=price_output if price_output is not None else config.COST_PRICE_OUTPUT,
            )
        return _budgets[session_id]


def reset_budgets():
    """测试用：清空预算注册表。"""
    global _budgets
    with _budgets_lock:
        _budgets = {}
