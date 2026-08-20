"""★ W28-D1 容器口径统一（C1）：语义缓存容器内命中验证（真实 bge embedding）。

在 backend 容器内运行：python /app/verify_semcache_container.py
验证点：
1. SemanticCache 用真实 bge 模型 embedding，相似问（双闸门）命中；
2. 无关问不命中；Redis 权威可用；验证后清理缓存。
"""
from app.shared.rag.semantic_cache import SemanticCache

c = SemanticCache()
c.put("采购申请需要经过哪几级审批", "三级审批：需求部门负责人+采购经理+分管副总。",
      citations=[{"doc_id": "SCM-PUR-001"}])
h1 = c.lookup("采购申请要经过哪几级审批？")
print("HIT1(should hit):", bool(h1), h1 and (h1["sim"], h1["char_overlap"]))
h2 = c.lookup("今天天气怎么样？")
print("HIT2(should miss):", bool(h2))
print("redis_available:", c.rc.available)
print("hit_rate:", c.hit_rate())
c.clear()
print("OK")
