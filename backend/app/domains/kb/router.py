"""知识问答域路由（W23 Day4 由 stage3-a `server.py` 改造为平台模块化单体）。

迁移要点（对应手册 Day4）：
- FastAPI app → APIRouter，挂载前缀 `/api/kb`（main.py include_router）
- 认证统一走平台：`require_permission("kb:chat")` / `require_permission("kb:feedback")`
  （原 server.py 的 /auth/* 登录、/health、/metrics 由平台基座接管，此处删除）
- 审计统一走平台：`app.platform.audit.write_audit`（原 security.audit 双份实现移除）
- 观测保留 shared.obs（结构化日志/指标/OTEL 在平台 main 统一初始化）

问答链路（零业务逻辑改动）：输入消毒 → 语义缓存 → 语义路由 → 混合检索 →
生成+双校验（CRAG/缺失回退）→ 流式返回 + 引用溯源 → 审计留痕。
SSE 事件契约与 stage3 一致（progress / message / citations / done / error）。
"""

import asyncio
import contextlib
import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.domains.kb import config
from app.domains.kb.agent.answer_validator import generate_with_validation
from app.domains.kb.feedback.feedback_store import FeedbackStore
from app.domains.kb.security.input_sanitizer import InputSanitizer
from app.platform import rbac
from app.platform.audit import write_audit
from app.platform.conversation import touch_conversation
from app.platform.models import User
from app.shared.obs import logger as obs_logger
from app.shared.rag.semantic_cache import SemanticCache
from app.shared.rag.semantic_router import SemanticRouter, log_route

router = APIRouter(prefix="/api/kb", tags=["kb"])

# 结构化日志（平台 main 已统一初始化 obs，此处取 logger）
_log = obs_logger.get_logger("kb")

# 进程级单例（模型/BM25 索引懒加载，避免冷启动全量加载——保留 stage3 设计）
_retriever = None
_provider = None
_semantic_router = None
_semantic_cache = None

sanitizer = InputSanitizer()
feedback_store = FeedbackStore()


def get_semantic_router():
    """语义路由器（懒加载）。"""
    global _semantic_router
    if _semantic_router is None:
        _semantic_router = SemanticRouter()
    return _semantic_router


def get_semantic_cache():
    """语义缓存（懒加载）。"""
    global _semantic_cache
    if _semantic_cache is None:
        _semantic_cache = SemanticCache()
    return _semantic_cache


def get_provider_safe():
    """LLM Provider（带降级保护）：real 配置缺失/异常 → mock 兜底，服务不崩。"""
    global _provider
    if _provider is None:
        try:
            from app.shared.llm import get_provider

            _provider = get_provider()
        except Exception as e:
            print(f"[kb] LLM Provider 初始化失败（{type(e).__name__}: {str(e)[:80]}）"
                  f"→ 降级 mock")
            from app.shared.llm import get_provider

            _provider = get_provider("mock")
        print(f"[kb] LLM Provider = {_provider.name}")
    return _provider


def get_retriever():
    """混合检索器（懒加载：首次调用才建 BM25 索引 + 模型加载）。"""
    global _retriever
    if _retriever is None:
        from app.shared.rag.hybrid_retriever import HybridRetriever
        from app.shared.rag.reranker import get_reranker

        _retriever = HybridRetriever(reranker=get_reranker())
    return _retriever


# ==================== 平台审计适配 ====================


async def _audit(request: Request, event: str, detail: str = "", **kw) -> None:
    """写一条平台审计（event 级业务事件）；失败不阻塞业务（审计旁路）。"""
    import contextlib

    with contextlib.suppress(Exception):  # 审计尽力而为
        factory = request.app.state.session_factory
        extra = kw.get("extra") or {}
        payload: dict = {"message": detail}
        if extra:
            payload.update(extra)
        async with factory() as session:
            await write_audit(session, event=event, target=request.url.path, detail=payload)
            await session.commit()


def _audit_sink(request: Request):
    """log_route 的审计 sink（同步接口 → 异步写，不阻塞路由决策）。"""
    import contextlib

    def _sink(event: str, **kw) -> None:
        with contextlib.suppress(Exception):  # 审计尽力而为
            asyncio.create_task(_audit(request, event, kw.get("detail", "")))

    return _sink


def _ss(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# ==================== 路由 ====================


@router.post("/chat")
async def chat(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("kb:chat"))],
):
    """SSE 流式问答（progress → message → citations → done）。需要权限 kb:chat。"""
    body = await request.json()
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id") or str(uuid.uuid4())
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)
    if len(message) > 2000:
        return JSONResponse({"ok": False, "error": "message too long (max 2000)"},
                            status_code=400)
    request_id = str(uuid.uuid4())[:12]

    async def event_gen():
        try:
            obs_logger.log_event(_log, "chat_started", request_id=request_id,
                                 session_id=session_id, msg_len=len(message))
            # ① 会话历史落库（★ W23 Day5：conversations 表，多轮追问数据源；尽力而为）
            with contextlib.suppress(Exception):
                await touch_conversation(
                    request.app.state.session_factory,
                    thread_id=session_id,
                    user_id=current.id,
                    tenant_id=current.tenant_id,
                    title=message[:50],
                )

            # ① 输入消毒（规则层，A7 双层的规则部分；命中 → 拦截 + 审计）
            scan = sanitizer.scan(message)
            if scan["flagged"]:
                await _audit(request, "input_block", "注入规则命中",
                             extra={"match": scan["hits"][0]["match"][:40]})
                obs_logger.log_event(_log, "input_blocked", level="warning",
                                     request_id=request_id, session_id=session_id)
                yield _ss({"type": "error", "error": "已拦截疑似注入内容"})
                yield _ss({"type": "done"})
                return

            # ①.5 语义缓存优先——语义相似问题命中则直接返回（省 RAG + LLM token）
            if config.SEMANTIC_CACHE_ENABLED:
                cached = get_semantic_cache().lookup(message)
                if cached:
                    await _audit(request, "semantic_cache_hit",
                                 f"sim={cached['sim']} 命中缓存，省 RAG+LLM")
                    obs_logger.log_event(_log, "semantic_cache_hit", request_id=request_id,
                                         session_id=session_id, sim=cached["sim"],
                                         matched_query=cached.get("matched_query", ""))
                    yield _ss({"type": "progress", "node": "cache",
                               "data": {"result": f"语义缓存命中（相似度 {cached['sim']}），直接返回缓存回答"}})
                    ans = cached["answer"]
                    for i in range(0, len(ans), 12):
                        yield _ss({"type": "message", "role": "assistant",
                                   "content": ans[i:i + 12], "delta": True,
                                   "session_id": session_id})
                        await asyncio.sleep(0.02)
                    yield _ss({"type": "message", "role": "assistant", "content": "",
                               "delta": False, "session_id": session_id})
                    yield _ss({"type": "citations", "citations": cached["citations"],
                               "retrieved_docs": [],
                               "validation": {"passed": True, "retries": 0,
                                              "degraded": False, "warning": ""},
                               "source": "cache", "session_id": session_id})
                    yield _ss({"type": "done"})
                    return

            # ①.6 语义路由分流——让请求只走该走的链路（不相关请求不进 RAG）
            if config.SEMANTIC_ROUTER_ENABLED:
                route_res = get_semantic_router().route(message)
                log_route(message, route_res, sink=_audit_sink(request))
                if route_res["route"] != "rag":
                    obs_logger.log_event(_log, "semantic_route", request_id=request_id,
                                         session_id=session_id,
                                         route=route_res["route"], score=route_res["score"])
                    yield _ss({"type": "progress", "node": "route",
                               "data": {"result": f"语义路由 → {route_res['route']}（score={route_res['score']}）"}})
                    if route_res["route"] == "chat":
                        answer = "你好呀！我是供应链知识库问答助手，专注制度条文解答。如需业务操作（查单/改单/报表），请前往业务助手；如需制度咨询，直接问我就好。"
                        yield _ss({"type": "message", "role": "assistant", "content": answer,
                                   "delta": False, "session_id": session_id})
                        yield _ss({"type": "citations", "citations": [],
                                   "retrieved_docs": [],
                                   "validation": {"passed": True, "retries": 0,
                                                  "degraded": False, "warning": ""},
                                   "source": "route", "session_id": session_id})
                        yield _ss({"type": "done"})
                        return
                    if route_res["route"] == "data":
                        # ★ W24 Day6：data 分支打通——查数问题 → NL2SQL 域 → 流式 data_table 事件
                        #   权限：NL2SQL 需要 data:nl2sql（analyst/admin）；此处对话入口已过 kb:chat，
                        #   再按权限码二次校验（viewer 无 data:nl2sql → 礼貌拒答，不泄露 SQL）
                        if "data:nl2sql" not in rbac.current_permissions(current):
                            yield _ss({"type": "message", "role": "assistant",
                                       "content": "查数（NL2SQL）需要数据查询权限，请联系管理员开通。",
                                       "delta": False, "session_id": session_id})
                            yield _ss({"type": "done"})
                            return
                        yield _ss({"type": "progress", "node": "nl2sql",
                                   "data": {"result": "已转交数据分析域（NL2SQL），正在查询…"}})
                        with contextlib.suppress(Exception):
                            await _audit(request, "nl2sql_route", "语义路由 data 分支转 NL2SQL")

                        async def _nl2sql_audit_sink(event: dict) -> None:
                            """符合 data executor 审计回调契约（{event,sql,status,error,...}）→ 平台审计。"""
                            with contextlib.suppress(Exception):
                                await _audit(
                                    request, event.get("event", "data:nl2sql:execute"),
                                    f"sql={event.get('sql', '')[:200]} status={event.get('status')} "
                                    f"rows={event.get('rows')}",
                                )

                        from app.domains.data.service import run_nl2sql_query

                        res = await run_nl2sql_query(
                            question=message,
                            session_id=session_id,
                            audit_sink=_nl2sql_audit_sink,
                        )
                        if res["table"]:
                            yield _ss({
                                "type": "data_table",
                                "columns": res["columns"],
                                "rows": res["rows"],
                                "sql": res["sql"],
                                "insights": res["insights"],
                                "elapsed": res["elapsed"],
                                "truncated": res["truncated"],
                                "rejected_reason": res["rejected_reason"],
                                "reply": res["reply"],
                                "session_id": session_id,
                            })
                        else:
                            yield _ss({"type": "message", "role": "assistant",
                                       "content": res["reply"] or "暂时无法生成有效查询，请换一种问法。",
                                       "delta": False, "session_id": session_id})
                        yield _ss({"type": "done"})
                        return
                    # tool
                    answer = "这是业务操作请求（查单/改单/报表），应转交业务助手（项目 B）处理。本知识库助手专注制度条文问答，如需制度条文请直接提问。"
                    yield _ss({"type": "message", "role": "assistant", "content": answer,
                               "delta": False, "session_id": session_id})
                    yield _ss({"type": "citations", "citations": [],
                               "retrieved_docs": [],
                               "validation": {"passed": True, "retries": 0,
                                              "degraded": False, "warning": ""},
                               "source": "route", "session_id": session_id})
                    yield _ss({"type": "done"})
                    return

            # ② 混合检索（BM25 + 向量 + RRF + 重排）
            retriever = get_retriever()
            hits = retriever.retrieve(message, top_k=5)
            docs = list(dict.fromkeys(h["doc_id"] for h in hits))
            yield _ss({"type": "progress", "node": "retrieve",
                       "data": {"result": f"混合检索命中 {len(hits)} 个候选（涉及 {len(docs)} 篇文档）"}})

            # ③ 生成 + 校验（规则 + LLM 双校验 + 缺失回退）
            yield _ss({"type": "progress", "node": "generate", "data": {"result": "生成回答（含引用校验）"}})
            provider = get_provider_safe()
            result = await generate_with_validation(provider, message, hits)
            answer = str(result.get("answer", ""))
            citations = result.get("citations") or []
            validation = result.get("validation") or {}
            if not validation.get("passed"):
                await _audit(request, "validator_fail", validation.get("reason", "")[:200])
                obs_logger.log_event(_log, "validator_fail", level="warning",
                                     request_id=request_id, session_id=session_id,
                                     reason=validation.get("reason", "")[:120])

            # ③.5 生成结果写入语义缓存（校验通过的答案才缓存，防污染）
            if config.SEMANTIC_CACHE_ENABLED and validation.get("passed") and answer:
                get_semantic_cache().put(message, answer, citations)

            # ④ 流式回答（打字机效果）
            for i in range(0, len(answer), 12):
                yield _ss({"type": "message", "role": "assistant",
                           "content": answer[i:i + 12], "delta": True,
                           "session_id": session_id})
                await asyncio.sleep(0.02)
            yield _ss({"type": "message", "role": "assistant", "content": "",
                       "delta": False, "session_id": session_id})

            # ⑤ 引用溯源 + 校验结果
            yield _ss({"type": "citations", "citations": citations,
                       "retrieved_docs": docs,
                       "validation": {"passed": validation.get("passed", False),
                                      "retries": result.get("retries", 0),
                                      "degraded": result.get("degraded", False),
                                      "warning": result.get("warning", "")},
                       "session_id": session_id})
            yield _ss({"type": "done"})
            obs_logger.log_event(_log, "chat_done", request_id=request_id,
                                 session_id=session_id, status=200)
        except Exception as e:
            obs_logger.log_event(_log, "chat_error", level="error",
                                 request_id=request_id, session_id=session_id,
                                 error=f"{type(e).__name__}: {str(e)[:120]}")
            print(f"[kb] /api/kb/chat 异常: {type(e).__name__}: {str(e)[:200]}")
            yield _ss({"type": "error", "error": str(e)[:200]})
            yield _ss({"type": "done"})

    response = StreamingResponse(event_gen(), media_type="text/event-stream")
    response.headers["X-Session-Id"] = session_id
    response.headers["Cache-Control"] = "no-cache"
    return response


@router.post("/feedback")
async def feedback(
    request: Request,
    current: Annotated[User, Depends(rbac.require_permission("kb:feedback"))],
):
    """反馈闭环：点赞/纠错 → 待审核 → 管理员审核 → 回流评测集 v2。需要权限 kb:feedback。

    ★ 平台化：原 stage3 的 admin/operator 角色限制升级为权限码 `kb:feedback`
      （仅 admin/operator 拥有，viewer 403——RBAC 矩阵语义不变）。
    """
    body = await request.json()
    try:
        rec = feedback_store.submit(
            user_id=current.username,  # 写操作身份来自 token（防伪造）
            question=(body.get("question") or "").strip(),
            action=body.get("action", "like"),
            original_answer=body.get("original_answer", ""),
            corrected_answer=body.get("corrected_answer", ""),
            correct_doc_ids=body.get("correct_doc_ids") or [],
            qa_id=body.get("qa_id", ""),
        )
        await _audit(request, "feedback_submitted", f"action={rec['action']}")
        return {"ok": True, "feedback_id": rec["feedback_id"], "status": rec["status"]}
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
