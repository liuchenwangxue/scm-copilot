"""★ 审批 IM 推送最小版（W28 Day5，C6/B6 项）：挂 `approval_requested` 钩子。

目标（半小时版，面试可讲"通知尽力而为，审批状态机不受影响"）：
- 审批发起（ApprovalService.create()）→ POST 群机器人 webhook 摘要卡片
- **不发敏感值**：只发审批 id + 工具名 + 变更字段名（before/after 的值不进群）
- 3s 超时 + 1 次重试后放弃（尽力而为）；webhook 挂 → 审批照常，仅无通知
- `SCM_WEBHOOK_URL` 空 = 关闭（默认关闭，容器/本地可注入）
- 无真群：本地用 echo 服务模拟（手册允许）；单测用 httpbin/mock 断言

设计（对齐 w6 重试哲学 + hooks 故障放行原则）：
- 通知是**旁路副作用**：try/except 全包，任何异常不影响审批主流程
- 同步阻塞控制在超时内（3s）；异步 fire-and-forget（线程池）避免阻塞审批请求
- 摘要卡片 JSON 结构贴近企微/钉钉群机器人（msgtype=text 最简；markdown 留二期）
"""
from __future__ import annotations

import json
import logging
import os
import threading

import httpx

logger = logging.getLogger("scm.ops.notify.webhook")

# 群机器人 webhook 地址（企微/钉钉群机器人 URL；空 = 关闭推送）
# 环境变量示例：SCM_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
WEBHOOK_URL = os.getenv("SCM_WEBHOOK_URL", "").strip()

_TIMEOUT = float(os.getenv("SCM_WEBHOOK_TIMEOUT", "3"))
_MAX_RETRIES = 1
# 敏感字段：摘要只列字段名，不携带值（金额/日期/原因均不外泄到群）
_SENSITIVE_KEYS = {"amount", "delivery_date", "reason"}


def _build_card(approval_id: str, tool_name: str, operation: str,
                diff_fields: list[str], order_id: str) -> dict:
    """摘要卡片：审批 id + 工具 + 变更字段名（不落敏感值）。

    msgtype=text 兼容企微/钉钉群机器人最简协议；markdown 富文本列二期。
    """
    lines = [
        "【SCM 审批提醒】新审批待处理",
        f"- 审批单号：{approval_id[:8]}",
        f"- 操作：{operation}（{tool_name}）",
        f"- 目标订单：{order_id}",
        f"- 变更字段：{'、'.join(diff_fields) or '无'}",
        f"- 详情：请登录 SCM 平台处理（ID: {approval_id}）",
    ]
    return {"msgtype": "text", "text": {"content": "\n".join(lines)}}


def diff_field_names(diff: list[dict]) -> list[str]:
    """从审批 diff 列表提取字段名（不含值，防敏感外泄）。"""
    fields = []
    for d in diff or []:
        f = d.get("field")
        if f and f not in fields:
            fields.append(f)
    return fields


def send_approval_webhook(approval_id: str, tool_name: str, operation: str,
                          order_id: str, diff: list[dict] | None = None,
                          reason: str = "") -> bool:
    """发送审批摘要到群机器人。返回是否送达（webhook 关/失败 → False）。

    尽力而为：3s 超时 + 1 次重试，任何异常记日志放行——审批状态机不受影响。
    """
    if not WEBHOOK_URL:
        return False
    fields = diff_field_names(diff or [])
    payload = _build_card(approval_id, tool_name, operation, fields, order_id)
    headers = {"Content-Type": "application/json"}

    last_err: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            r = httpx.post(WEBHOOK_URL, json=payload, headers=headers,
                           timeout=_TIMEOUT)
            if r.status_code < 300:
                logger.info("webhook sent: %s approval=%s", tool_name, approval_id)
                return True
            last_err = RuntimeError(f"webhook status {r.status_code}: {r.text[:120]}")
        except Exception as e:  # noqa: BLE001  # 通知尽力而为
            last_err = e
        if attempt < _MAX_RETRIES:
            logger.warning("webhook retry %s/1: %s", attempt + 1, last_err)
    logger.warning("webhook failed after %s retries: %s", _MAX_RETRIES, last_err)
    return False


def notify_approval_requested_async(approval_id: str, tool_name: str,
                                    operation: str, order_id: str,
                                    diff: list[dict] | None = None,
                                    reason: str = "") -> None:
    """异步 fire-and-forget 推送（线程池）——不阻塞审批 create 主流程。

    webhook 关闭（SCM_WEBHOOK_URL 空）时零开销直接返回。
    """
    if not WEBHOOK_URL:
        return
    threading.Thread(
        target=send_approval_webhook,
        args=(approval_id, tool_name, operation, order_id),
        kwargs={"diff": diff, "reason": reason},
        daemon=True,
    ).start()
