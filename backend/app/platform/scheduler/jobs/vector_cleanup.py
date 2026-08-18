"""vector_cleanup：向量卫生清理（每日 03:00，W25 Day2 实现）。

cron: 0 3 * * *
作用：孤儿向量（payload source_doc_id 已不在 docs 表）删除 + 语义缓存过期键扫描
（TTL 漏网 + 版本失效标记）。

Day1 为占位实现，真实逻辑见 W25 Day2。
"""

CRON = "0 3 * * *"


async def run() -> dict:
    # TODO(W25 Day2): 孤儿向量清理 + 语义缓存失效键扫描
    return {"job": "vector_cleanup", "status": "stub", "note": "implemented in W25 Day2"}
