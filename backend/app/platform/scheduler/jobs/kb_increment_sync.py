"""★ kb_increment_sync：知识库增量同步（*/5min，W25 Day2 实现）。

cron: */5 * * * *
作用：docs 元数据表扫 mtime > last_sync_ts → 变更文档重切块重嵌入 → Qdrant upsert
（uuid5 内容寻址幂等）；删除文档 → 按 payload source_doc_id 删向量。
last_sync_ts 存 Redis（任务幂等键，失败不推进水位）。

Day1 为占位实现，真实逻辑见 W25 Day2。
"""

CRON = "*/5 * * * *"


async def run() -> dict:
    # TODO(W25 Day2): 增量同步实现——mtime 扫描 → 重切块 → 嵌入 → Qdrant upsert/delete
    return {"job": "kb_increment_sync", "status": "stub", "note": "implemented in W25 Day2"}
