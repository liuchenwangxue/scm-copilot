"""★ W28-D1：语义路由分类验证（容器内，真实 bge embedding）。"""
import time

from app.domains.kb.router import get_semantic_router

r = get_semantic_router()
for q in ["你好呀，你能做什么？", "你好", "早上好呀", "帮我查一下订单 PO-0003 现在到哪了",
          "采购申请需要经过哪几级审批"]:
    t0 = time.time()
    res = r.route(q)
    print(f"{q!r} -> route={res['route']} src={res['source']} score={res['score']} "
          f"elapsed={round(time.time()-t0, 3)}s")
