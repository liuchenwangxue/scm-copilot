"""进程级模型状态注册表（★ W28-D1：容器口径统一，/health 暴露模型降级状态）。

背景：W28 把 embedding/reranker 真模型装进容器（C1），但保留"降级哲学"——
模型加载失败（卷未挂 / 下载不全 / 缺依赖）必须自动回退 mock/RuleReranker，
且状态要可观测（/health 可见 embedder=degraded）。

设计：
- Embedder / BGEReranker 都是懒加载（首次构建才加载模型），/health 作为探针
  要在不依赖业务请求的情况下报告"模型是否可用" → 本模块维护进程级状态注册表。
- 状态机：
    embedder: pending → real / mock（主动选择）/ mock_degraded（real 加载失败降级）
    reranker: pending → bge(...) / rule / bge-failed→rule（bge 加载失败降级）
- /health 首次调用时若状态仍 pending，主动探测一次（实例化触发加载/降级），
  之后直接读缓存（幂等，避免每次探活都重载模型）。
- 纯 dict 进程级状态，无锁（GIL 下赋值原子；单进程单实例语义，双实例各自上报）。
"""

_STATE: dict[str, dict[str, str | None]] = {
    "embedder": {"mode": "pending", "load_error": None},
    "reranker": {"mode": "pending", "load_error": None},
}


def record_embedder(mode: str, load_error: str | None = None) -> None:
    """记录 embedding 状态（由 Embedder 构造时调用）。"""
    _STATE["embedder"] = {"mode": mode, "load_error": load_error}


def record_reranker(mode: str, load_error: str | None = None) -> None:
    """记录重排器状态（由 BGEReranker/RuleReranker 探测时调用）。"""
    _STATE["reranker"] = {"mode": mode, "load_error": load_error}


def embedder_status() -> str:
    mode = _STATE["embedder"]["mode"]
    return mode or "pending"


def reranker_status() -> str:
    mode = _STATE["reranker"]["mode"]
    return mode or "pending"


def snapshot() -> dict:
    """/health 用快照（不携带敏感细节）。"""
    return {
        "embedder": embedder_status(),
        "reranker": reranker_status(),
    }


def probe_if_pending() -> None:
    """首次 /health 探测：实例化 Embedder / 重排器触发加载或降级，结果进程内缓存。

    幂等：状态非 pending 后不再触发（/health 高频探活不重载模型）。
    probe 会在进程内同步加载一次模型（bge-small ~3s、bge-reranker ~5-10s），
    docker healthcheck 的 start_period 已按此放宽（见 compose）。
    """
    if embedder_status() == "pending":
        from app.shared.rag.embedder import Embedder

        Embedder()  # __init__ 内 real 预检加载；失败自动降级 mock 并 record
    if reranker_status() == "pending":
        from app.shared.rag.reranker import get_reranker

        get_reranker().status()  # 触发加载探测并 record
