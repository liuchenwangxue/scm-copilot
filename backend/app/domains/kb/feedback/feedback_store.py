"""反馈闭环（★ W18 Day6，欠账 A6 同周补完配套）。

定位：把"能问答"升级为"能自我进化"的系统——用户点赞/纠错 → 管理员审核 →
通过则回流评测集 → 重跑基线看 Δ。

字段设计（写进 reports/day6_feedback_design.md）：
    feedback_id       反馈唯一 ID（uuid）
    qa_id             关联的原 QA id（可为空：纯新增纠错）
    user_id           提交反馈的用户（去敏，仅角色/编号）
    action            like | dislike | correction
    original_answer   原回答（纠正时留档）
    corrected_answer  纠正后的标准答案（correction 必填）
    correct_doc_ids   纠正涉及的文档（source_doc_ids）
    question          对应问题
    timestamp         提交时间
    status            pending(待审) | approved(已回流) | rejected(驳回)
    source            feedback（回流评测集时打标，防评测集污染）

防评测集污染（面试细节加分）：回流的新 QA 必须带 source=feedback，
统计基线时可选择排除 source=feedback 的条目，保证"自进化不影响基线可比性"。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path

from app.domains.kb import config

# 反馈落盘文件（JSON lines）
FEEDBACK_FILE = config.REPORTS_DIR / "feedback_store.jsonl"
# 回流评测集 v2（从原评测集 + 审核通过反馈合并生成）
EVAL_V2_FILE = config.DATA_DIR / "qa_eval_set.v2.json"


class FeedbackStore:
    """反馈闭环存储 + 审核回流流程。"""

    def __init__(self, path: Path | None = None, qa_eval_file=None):
        self.path = Path(path) if path else FEEDBACK_FILE
        self.qa_eval_file = Path(qa_eval_file) if qa_eval_file else Path(config.QA_EVAL_FILE)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 写入 ----------

    def submit(self, *, user_id: str, question: str, action: str,
               original_answer: str = "", corrected_answer: str = "",
               correct_doc_ids: list | None = None, qa_id: str = "") -> dict:
        """提交一条反馈。action: like|dislike|correction。"""
        if action not in ("like", "dislike", "correction"):
            raise ValueError(f"action 必须为 like/dislike/correction，收到: {action}")
        if action == "correction" and not corrected_answer.strip():
            raise ValueError("correction 反馈必须提供 corrected_answer")

        rec = {
            "feedback_id": f"fb_{uuid.uuid4().hex[:8]}",
            "qa_id": qa_id,
            "user_id": user_id,
            "action": action,
            "question": question,
            "original_answer": original_answer,
            "corrected_answer": corrected_answer,
            "correct_doc_ids": correct_doc_ids or [],
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "status": "pending",
            "source": "feedback",
        }
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def reset(self) -> None:
        """清空反馈存储（仅测试/demo 用：截断 jsonl，不删文件）。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write("")

    # ---------- 查询 ----------

    def list_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        recs = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return recs

    def pending(self) -> list[dict]:
        return [r for r in self.list_all() if r["status"] == "pending"]

    # ---------- 审核 ----------

    def review(self, feedback_id: str, approved: bool, reviewer: str = "admin") -> dict | None:
        """管理员审核：approved=True → 更新状态为 approved（待回流）；False → rejected。"""
        recs = self.list_all()
        target = next((r for r in recs if r["feedback_id"] == feedback_id), None)
        if target is None:
            return None
        target["status"] = "approved" if approved else "rejected"
        target["reviewed_by"] = reviewer
        target["reviewed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._write_all(recs)
        return target

    def _write_all(self, recs: list[dict]) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            for r in recs:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ---------- 回流评测集（防污染） ----------

    def flow_back_to_eval(self, approved_ids: list[str] | None = None) -> dict:
        """把审核通过的 correction 回流到评测集 v2。

        - 只回流 action=correction 且 approved 的反馈（like/dislike 不进评测集）
        - 新 QA 标 source=feedback（防污染）；原评测集条目标 source=base
        - 输出 qa_eval_set.v2.json + 返回统计
        """
        base = json.loads(self.qa_eval_file.read_text(encoding="utf-8"))
        base = [{**q, "source": q.get("source", "base")} for q in base]
        existing_ids = {q["id"] for q in base}

        approved = [r for r in self.list_all()
                    if r["status"] == "approved" and r["action"] == "correction"]
        if approved_ids:
            approved = [r for r in approved if r["feedback_id"] in approved_ids]

        new_items = []
        for fb in approved:
            if fb["question"] in existing_ids:
                continue
            qa = {
                "id": fb["feedback_id"],
                "question": fb["question"],
                "answer": fb["corrected_answer"],
                "source_doc_ids": fb["correct_doc_ids"] or [],
                "category": "feedback",
                "difficulty": "unknown",
                "type": "single",
                "source": "feedback",
            }
            new_items.append(qa)
            existing_ids.add(fb["question"])

        merged = base + new_items
        EVAL_V2_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding="utf-8")
        return {
            "base_count": len(base),
            "flowed_back": len(new_items),
            "total_v2": len(merged),
            "v2_file": str(EVAL_V2_FILE),
            "flowed_back_items": new_items,
        }

    # ---------- 统计 ----------

    def stats(self) -> dict:
        recs = self.list_all()
        return {
            "total": len(recs),
            "by_action": {a: sum(1 for r in recs if r["action"] == a)
                          for a in ("like", "dislike", "correction")},
            "by_status": {s: sum(1 for r in recs if r["status"] == s)
                          for s in ("pending", "approved", "rejected")},
        }
