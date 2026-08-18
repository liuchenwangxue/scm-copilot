"""RQ 队列封装（★ W22 Day4：报表异步化，broker=Redis）。

职责：
- enqueue_report：把报表生成投到 RQ 队列，返回 task_id（= job.id）
- get_report_result：按 task_id 轮询结果（finished → result；failed → exc_info）
- sync_generate_report：同步降级（Redis/队列不可用或 TASK_QUEUE_ENABLED=0 时直接跑）

设计（对应手册坑"队列挂→报表同步降级"）：
- fail-open：Redis 不可用 → sync 同步跑（API 仍能用，只是不退后台）
- RQ job 用字符串路径引用（"tasks.report_job.generate_report_job"）→ worker 独立 import
- 结果 TTL（result_ttl）保留一段时间供轮询；job 超时默认 300s（报表 LLM 生成够用）
- Windows 启动 worker：cd 到 backend 目录，PYTHONPATH 指向 backend，rq worker report

验证（本项目测试脚本）：
    python -X utf8 stage3-project-b/scripts/day4_queue_test.py   # 需 Redis（16380）
"""
import json

from app.domains.ops import config
from app.shared.reliability.redis_client import get_redis_client

QUEUE_NAME = "report"
JOB_PATH = "app.domains.ops.tasks.report_job.generate_report_job"


class ReportTaskQueue:
    """报表异步任务队列（RQ）。Redis 不可用时自动同步降级。"""

    def __init__(self, url: str | None = None, enabled: bool | None = None):
        self.enabled = config.TASK_QUEUE_ENABLED if enabled is None else enabled
        self.rc = get_redis_client()  # reliability 封装（fail-open + 健康缓存）
        self._queue = None

    # ---- 连接 ----

    def _get_queue(self):
        """懒建 RQ Queue。不可用返回 None。

        ★ 关键坑：redis-py 8.x 默认 RESP3，RQ 2.x 期望 RESP2——用 reliability 的
        rc._connect()（默认 RESP3）会让 RQ 反序列化 job 时 UnicodeDecodeError。
        因此 RQ 专用独立连接，显式 protocol=2（不影响 reliability 的其他模块）。
        """
        if not self.enabled or not self.rc.available:
            return None
        if self._queue is None:
            import redis as _redis
            from rq import Queue
            # RQ 2.x 期望：protocol=2（redis-py 8 默认 RESP3 会反序列化报错）+
            # decode_responses=False（RQ 内部自己 decode，设 True 会让其 .decode() 报错）
            self._conn = _redis.Redis.from_url(
                self.rc.url,
                socket_connect_timeout=self.rc.timeout,
                socket_timeout=self.rc.timeout,
                decode_responses=False,
                protocol=2,
            )
            self._queue = Queue(QUEUE_NAME, connection=self._conn)
        return self._queue

    # ---- 投递 ----

    def enqueue_report(self, report_type: str, from_date: str | None = None,
                       to_date: str | None = None) -> dict:
        """投递报表任务 → 立即返回 task_id。Redis 不可用 → 同步跑并标记 source=sync。

        返回: {"task_id": str|None, "sync": bool, "async": bool}
            - async=True：已投队列，等轮询
            - sync=True：同步降级直接跑（task_id=None）
        """
        q = self._get_queue()
        if q is None:
            # 队列不可用 → 同步降级（fail-open，不丢需求）
            result = self.sync_generate_report(report_type, from_date, to_date)
            return {"task_id": None, "async": False, "sync": True, "result": result}
        try:
            job = q.enqueue(JOB_PATH, report_type, from_date, to_date,
                            job_timeout=config.TASK_QUEUE_JOB_TIMEOUT,
                            result_ttl=config.TASK_QUEUE_RESULT_TTL,
                            failure_ttl=config.TASK_QUEUE_RESULT_TTL)
            # ★ W26 Day1：入队成功即更新队列深度 Gauge（Grafana "队列与调度" 面板）
            try:
                from app.shared.obs.metrics import set_rq_queue_depth
                set_rq_queue_depth(QUEUE_NAME, int(q.count or 0))
            except Exception:
                pass
            return {"task_id": job.id, "async": True, "sync": False, "result": None}
        except Exception as e:
            # 入队异常 → 同步降级
            result = self.sync_generate_report(report_type, from_date, to_date)
            return {"task_id": None, "async": False, "sync": True,
                    "result": result, "enqueue_error": str(e)[:80]}

    # ---- 轮询 ----

    def get_report_result(self, task_id: str) -> dict:
        """按 task_id 查任务状态与结果。未完成/不存在 → 返回 {status, ready: False}。

        完成（finished）→ 返回 {status:"finished", ready:True, result:{...}}
        失败（failed） → 返回 {status:"failed", ready:False, error:...}
        """
        try:
            from rq.job import Job  # RQ 2.x 从 rq.job 导入（rq 顶层不导出 Job）
            q = self._get_queue()
            if q is None:
                return {"status": "unknown", "ready": False, "reason": "queue disabled"}
            job = Job.fetch(task_id, connection=q.connection)
        except Exception as e:
            # Job 不存在（TTL 过期/未找到）
            return {"status": "not_found", "ready": False, "reason": str(e)[:80]}

        status = job.get_status()
        if status in ("queued", "started", "deferred", "scheduled"):
            return {"status": status, "ready": False}
        if status == "finished":
            return {"status": "finished", "ready": True,
                    "result": job.result if isinstance(job.result, dict) else {"raw": str(job.result)}}
        if status == "failed":
            return {"status": "failed", "ready": False,
                    "error": (job.exc_info or "job failed")[:300]}
        return {"status": status, "ready": False}

    # ---- 同步降级 ----

    @staticmethod
    def sync_generate_report(report_type: str, from_date: str | None = None,
                             to_date: str | None = None) -> dict:
        """同步直接跑（Redis 不可用 / 队列关闭时）。与 RQ job 同口径。"""
        from app.domains.ops.tasks.report_job import generate_report_job
        r = generate_report_job(report_type, from_date, to_date)
        r["sync"] = True
        return r


# 模块级单例
_default_queue = None


def get_queue() -> ReportTaskQueue:
    global _default_queue
    if _default_queue is None:
        _default_queue = ReportTaskQueue()
    return _default_queue
