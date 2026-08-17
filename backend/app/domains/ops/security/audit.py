"""★ A6 审计留痕（W19 Day4 补完）：操作类事件全量审计，JSON lines 落盘

审计事件链（一次高危操作 = 一条完整证据链）：
    approval_requested → approval_approved / approval_rejected → execution_succeeded / execution_failed
    + 幂等命中 idempotency_hit（重复提交被拦截的痕迹）

安全原则：
- 只记事件与业务对象 ID，**不记敏感内容**（Key/PII 不入日志）
- JSON lines 格式（每行一个 JSON 对象），标准审计格式，可被 logstash/splunk 直接消费
"""
import json
import threading
import time
from pathlib import Path


class AuditLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def log(self, event_type: str, **fields) -> dict:
        """写一条审计事件。返回写入的记录（含 event/ts）。"""
        record = {
            "event": event_type,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False)
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        print(f"  [AUDIT:{event_type}] " + " ".join(f"{k}={v}" for k, v in fields.items()))
        return record

    def read_all(self) -> list[dict]:
        """读全部审计记录（测试/报告用）。"""
        if not self.path.exists():
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    out.append(json.loads(line))
        return out

    def filter(self, event_type: str | None = None, **fields) -> list[dict]:
        records = self.read_all()
        if event_type:
            records = [r for r in records if r.get("event") == event_type]
        if fields:
            for k, v in fields.items():
                records = [r for r in records if r.get(k) == v]
        return records
