"""故障注入模块（W19 Day2，W23 Day6 随 SCM Copilot 部署复制）：环境变量驱动的接口故障模拟。

Day6 故障回归数据源。生产环境变量全部不设（默认无故障）。

| 环境变量 | 默认 | 说明 |
|---|---|---|
| BIZ_FAIL_RATE   | 0    | 0-1 随机失败率，命中 → 500 |
| BIZ_LATENCY_MS  | 0    | 每请求延迟毫秒数（模拟慢接口） |
| BIZ_500_MODE    | 空   | 逗号分隔路径前缀，命中 → 恒 500 |
| BIZ_429_MODE    | 空   | 逗号分隔路径前缀，命中 → 恒 429（模拟限流） |
"""
import os
import random
import time

FAIL_RATE = float(os.getenv("BIZ_FAIL_RATE", "0"))
LATENCY_MS = int(os.getenv("BIZ_LATENCY_MS", "0"))
MODE_500 = os.getenv("BIZ_500_MODE", "")
MODE_429 = os.getenv("BIZ_429_MODE", "")


def _match_prefix(path: str, spec: str) -> bool:
    """spec 为逗号分隔列表，支持两种匹配：
    - 精确匹配：path == token（如 `/api/v1/orders` 只命中列表，不命中详情）
    - 前缀匹配：token 以 `*` 结尾（如 `/api/v1/orders*` 命中列表+全部详情）
    """
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if token.endswith("*"):
            if path.startswith(token[:-1]):
                return True
        elif path == token:
            return True
    return False


def should_fail(path: str):
    """返回注入的故障类型：'500' | '429' | None（无故障）。"""
    if MODE_500 and _match_prefix(path, MODE_500):
        return "500"
    if MODE_429 and _match_prefix(path, MODE_429):
        return "429"
    if FAIL_RATE > 0 and random.random() < FAIL_RATE:
        return "500"
    return None


def maybe_latency() -> None:
    """按 BIZ_LATENCY_MS 模拟延迟（线程阻塞）。"""
    if LATENCY_MS > 0:
        time.sleep(LATENCY_MS / 1000.0)
