"""★ 任务队列包（W22 Day4：报表生成异步化，RQ + Redis broker）。

报表生成改异步：API 立即返回 {task_id} → worker（RQ）异步跑 → 完成回调/轮询。
- broker = Redis（W21 已有 redis 服务；复用 reliability.redis_client 连接）
- RQ 轻量 + Windows 友好（Celery 在 Windows worker 有子进程/信号问题，RQ 是线程模型）
- fail-open：Redis/队列不可用 → 同步降级直接跑（手册坑：队列挂→报表同步降级）

接口：
    queue.enqueue_report(report_type, from, to) -> str (task_id)  # 异步
    queue.get_report_result(task_id) -> dict|None                 # 轮询结果
    queue.sync_generate_report(report_type, from, to) -> dict     # 同步降级
"""
