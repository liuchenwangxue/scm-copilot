"""报表生成任务（★ W22 Day4：RQ worker 执行的 job）。

内容：拉报表数据（report_tools.generate_report，同步 HTTP）+ LLM 生成解读回答。
返回：{"success", "report_type", "data", "reply", "error"}——可被 API 轮询直接返回。

设计：
- 本模块必须能被 RQ worker 独立 import（不依赖 FastAPI app / 运行 loop）。
  因此不 import agent.graph（它含 async 图 + 单例副作用），而是直接复用
  report_tools（同步 HTTP）+ prompts.build_report_messages + llm.get_provider。
- 回答生成逻辑与 respond_node 保持一致（模板兜底 + real 失败降级模板），
  保证异步结果与同步链路口径一致。
- RQ worker 是线程模型：同步函数即可，LLM 调用内部各自处理（real 的 await 由
  asyncio.run 包住）。
"""
from app.domains.ops import config


def _build_reply(report_type: str, data: dict, error: str | None = None) -> str:
    """报表数据 → 中文回答（复用 agent.graph._template_report 逻辑，避免循环依赖）。"""
    from app.domains.ops.agent.graph import _template_report
    if error:
        return f"报表生成失败：{error}"
    return _template_report(data)


def generate_report_job(report_type: str, from_date: str | None = None,
                        to_date: str | None = None) -> dict:
    """RQ job：拉报表数据 + 生成回答。返回可轮询的完整结果 dict。"""
    from app.domains.ops.agent.tools.report_tools import ReportTools

    tools = ReportTools(config.BIZ_BASE_URL)
    result = tools.generate_report(report_type, from_date=from_date, to_date=to_date)

    if not result.success:
        return {"success": False, "report_type": report_type,
                "error": result.error, "reply": _build_reply(report_type, {}, result.error)}

    data = result.data or {}
    # 生成回答（与 respond_node 口径一致：mock → 模板；real → LLM，失败模板兜底）
    from app.domains.ops.agent.prompts import build_report_messages
    from app.shared.llm import get_provider

    provider = get_provider()
    reply = None
    if provider.name != "mock":
        try:
            import asyncio
            raw = asyncio.run(provider.generate(build_report_messages(data), max_tokens=512))
            if isinstance(raw, str) and not raw.startswith("[WARNING]"):
                reply = raw
        except Exception as e:
            print(f"[TASKS] 报表 LLM 生成失败，模板兜底: {e}")
    if reply is None:
        reply = _build_reply(report_type, data)

    return {"success": True, "report_type": report_type, "data": data,
            "reply": reply, "error": None,
            "source": result.meta.get("source", "live")}
