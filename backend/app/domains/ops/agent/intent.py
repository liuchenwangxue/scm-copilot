"""意图路由（W19 Day5）：LLM 识别 + 规则兜底（超预算降级/LLM 失败时）

手册坑：意图识别用真实 LLM 时中文订单号/日期提取易错——工具参数校验（pydantic）兜底，
错就回问用户（"请确认订单号"）。
"""
import re
from typing import Any

from app.domains.ops.agent.prompts import build_intent_messages

_ORDER_RE = re.compile(r"PO[-_]?\d{2,}", re.I)
_AMOUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(万元|块|元)?")

VALID_INTENTS = ("query_order", "update_order", "cancel_order", "generate_report", "unclear")

# 工具参数类型：order_id/delivery_date/reason 是 str，amount 是 float（混合值）
Params = dict[str, Any]


def extract_order_id(text: str) -> str | None:
    m = _ORDER_RE.search(text)
    if not m:
        return None
    # 归一化为 PO-0001 格式
    digits = re.sub(r"\D", "", m.group(0))
    return f"PO-{digits.zfill(4)}"


def rule_fallback(message: str) -> dict:
    """规则兜底（超预算降级 / LLM 不可用）：关键词匹配，覆盖率有限但稳定。"""
    m = message.lower()
    order_id = extract_order_id(message)
    if any(k in m for k in ("取消", "作废", "关闭")):
        return {"intent": "cancel_order", "params": {"order_id": order_id, "reason": message}}
    if any(k in m for k in ("改金额", "改交期", "修改", "调整", "改成", "改为")):
        params: Params = {"order_id": order_id}
        # ★ 先剔除订单号再匹配金额——否则 "PO-0002 改成 9500" 会把 0002 当金额
        body = _ORDER_RE.sub("", message)
        am = _AMOUNT_RE.search(body)
        if am and "交期" not in m:
            params["amount"] = float(am.group(1)) * (10000 if "万" in (am.group(2) or "") else 1)
        dm = re.search(r"(\d{4})-(\d{2})-(\d{2})", message)
        if dm:
            params["delivery_date"] = dm.group(0)
        return {"intent": "update_order", "params": params}
    if any(k in m for k in ("报表", "库存", "对账", "汇总")):
        rt = "inventory" if ("库存" in m or "仓储" in m) else "reconciliation"
        return {"intent": "generate_report", "params": {"report_type": rt}}
    if any(k in m for k in ("查", "订单", "状态", "详情", "到哪", "进度")):
        return {"intent": "query_order", "params": {"order_id": order_id}}
    return {"intent": "unclear", "params": {}}


class IntentRouter:
    """意图路由器：LLM 优先，规则兜底（use_llm=False 时纯规则——超预算降级路径）。"""

    def __init__(self, provider):
        self.provider = provider

    async def route(self, message: str, use_llm: bool = True,
                    token_sink=None) -> dict:
        """返回 {"intent", "params", "source": "llm"|"rule"}。

        token_sink: 可选回调 (prompt_tokens, completion_tokens) 累加预算。
        """
        if not use_llm:
            return {**rule_fallback(message), "source": "rule"}

        try:
            result = await self.provider.generate_json(
                build_intent_messages(message), {}, max_tokens=256)
            if token_sink:
                # 无真实 usage 时估算（预算兜底）；真实 LLM 的 usage 在 real_provider 已落盘
                token_sink(300, 60)
            # ★ 结构健壮性：real 降级到 mock 时返回 {"answer","citations"}（无 intent 键），
            #   或真实 LLM 返回不合规 JSON——一律回退规则兜底，不让 unclear 漏给用户
            if not isinstance(result, dict):
                print("  [INTENT] LLM 返回非 dict，规则兜底")
                return {**rule_fallback(message), "source": "rule"}
            # ★ 必须"确实返回了合法 intent"才信 LLM；缺失 intent 键（dict.get→None）→ 规则兜底
            #   （不能用 .get("intent", "unclear")——mock 的 {"answer","citations"} 会误判成 unclear）
            intent = result.get("intent")
            params = result.get("params") or {}
            if intent not in VALID_INTENTS:
                print(f"  [INTENT] LLM 未返回合法意图（{intent}），规则兜底")
                return {**rule_fallback(message), "source": "rule"}
            # 参数规范化：订单号格式统一
            if "order_id" in params and params["order_id"]:
                oid = extract_order_id(str(params["order_id"]))
                if oid is None:
                    oid = str(params["order_id"]).strip().upper()
                params["order_id"] = oid
            return {"intent": intent, "params": params, "source": "llm"}
        except Exception as e:
            print(f"  [INTENT] LLM 意图识别失败 ({e})，规则兜底")
            return {**rule_fallback(message), "source": "rule"}
