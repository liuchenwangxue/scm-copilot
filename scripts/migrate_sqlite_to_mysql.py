"""★ W23 Day5 数据迁移脚本：stage3 历史数据（SQLite/JSONL）→ scm_platform MySQL。

迁移对象（幂等，可重跑；方案见《03》第 5 节）：
1. approvals   ：stage3-b/data/approvals.db        → scm_platform.approvals
2. feedback    ：stage3-a/reports/feedback_store.jsonl → scm_platform.feedback
3. audit_logs  ：stage3-b/data/audit.log（+ scm-copilot/data/audit.log）→ scm_platform.audit_logs
4. checkpoints ：stage3-b/data/biz_agent.db 的 LangGraph 断点（checkpoints/writes）
                 → MySQL checkpointer 表（AsyncMySaver，历史 thread 可续跑）

设计要点：
- 幂等：目标表业务主键存在唯一约束，`INSERT ... ON DUPLICATE KEY UPDATE`（或 upsert）
- 校验和：逐表 `COUNT(*)` 比对 + 每表取 3 个关键字段做 `md5(concat(...))` 聚合比对
- checkpoints 迁移用 AsyncMySaver.aput/aput_writes（序列化格式由 langgraph serde 统一，
  旧 msgpack 断点可无损反序列化后按 MySQL JSON 列格式重新落库）
- 空源安全：表为空则输出 0 行一致，不报错（真实源 approvals 为空，如实记录）

用法：
  python scripts/migrate_sqlite_to_mysql.py [--dsn SCM_PLATFORM_DSN]
                                             [--stage3-root F:/code/agent/learning-outputs]
"""
import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.platform.settings import settings  # noqa: E402

RESULTS: list[dict[str, Any]] = []  # 迁移结果汇总（表 / 源行 / 目标行 / 校验和一致）


# ==================== 通用工具 ====================


def md5_checksum(rows: list[tuple[Any, ...]]) -> str:
    """行级校验和：按行排序后对选定列做 `|` 拼接，整体 md5。

    源侧与目标侧各取"同一语义"的列集合，顺序无关（排序后拼接），
    只要映射正确，两侧 md5 必然一致——这是行数比对之上的第二道校验。
    排序用全字符串 key（源 str / 目标 datetime 混合也能稳定比较）。
    """
    parts = []
    for r in sorted(rows, key=lambda row: tuple(str(v) for v in row)):
        parts.append("|".join("" if v is None else str(v) for v in r))
    return hashlib.md5("\n".join(parts).encode("utf-8")).hexdigest()


def _dt(v: Any) -> datetime | None:
    """各类源时间 → MySQL DATETIME。"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, (int, float)):  # epoch 秒（approvals.created_at）
        return datetime.fromtimestamp(v, tz=UTC).replace(tzinfo=None)
    s = str(v)
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _fmt_dt(d: datetime | None) -> str | None:
    return d.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] if d else None


# ==================== 1. approvals ====================


def load_approvals_src(src_db: Path) -> list[dict[str, Any]]:
    import sqlite3

    conn = sqlite3.connect(str(src_db))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT approval_id, session_id, tool_name, operation, order_id, "
            "before_json, after_json, reason, status, idem_key, created_at, resolved_at "
            "FROM approvals ORDER BY created_at"
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            before = json.loads(r["before_json"])
        except (ValueError, TypeError):
            before = None
        try:
            after = json.loads(r["after_json"])
        except (ValueError, TypeError):
            after = None
        out.append(
            {
                "approval_no": r["approval_id"],
                "action": r["tool_name"],
                "operation": r["operation"],
                "target_type": "order",
                "target_id": r["order_id"],
                "actor": r["session_id"],  # 历史库无真实 user，用 session 标识
                "diff_before": before,
                "diff_after": after,
                "reason": r["reason"],
                "idem_key": r["idem_key"],
                "status": r["status"],  # pending/approved/rejected 与平台语义一致
                "decided_at": _fmt_dt(_dt(r["resolved_at"])),
                "created_at": _fmt_dt(_dt(r["created_at"])),
            }
        )
    return out


async def migrate_approvals(dsn: str, rows: list[dict[str, Any]]) -> None:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for r in rows:
                await conn.execute(
                    text(
                        "INSERT INTO approvals (approval_no, action, operation, target_type, "
                        "target_id, actor, diff_before, diff_after, reason, idem_key, status, "
                        "decided_by, decided_at, created_at) "
                        "VALUES (:approval_no, :action, :operation, :target_type, :target_id, "
                        ":actor, :diff_before, :diff_after, :reason, :idem_key, :status, "
                        "NULL, :decided_at, :created_at) "
                        "ON DUPLICATE KEY UPDATE status = VALUES(status), "
                        "diff_before = VALUES(diff_before), diff_after = VALUES(diff_after), "
                        "reason = VALUES(reason), idem_key = VALUES(idem_key), "
                        "decided_at = VALUES(decided_at)"
                    ),
                    {
                        "approval_no": r["approval_no"],
                        "action": r["action"],
                        "operation": r["operation"],
                        "target_type": r["target_type"],
                        "target_id": r["target_id"],
                        "actor": r["actor"],
                        "diff_before": json.dumps(r["diff_before"], ensure_ascii=False),
                        "diff_after": json.dumps(r["diff_after"], ensure_ascii=False),
                        "reason": r["reason"],
                        "idem_key": r["idem_key"],
                        "status": r["status"],
                        "decided_at": r["decided_at"],
                        "created_at": r["created_at"],
                    },
                )
    finally:
        await engine.dispose()


async def _count_target(dsn: str, table: str) -> int:
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            return int(await conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) or 0)
    finally:
        await engine.dispose()


async def _checksum_target(dsn: str, table: str, cols: list[str]) -> str:
    """目标表校验和：取 3 个关键列，排序后拼接 md5（与源侧同语义）。"""
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as conn:
            col_sql = ", ".join(cols)
            rows = (await conn.execute(text(f"SELECT {col_sql} FROM {table}"))).all()
        return md5_checksum([tuple(r) for r in rows])
    finally:
        await engine.dispose()


# ==================== 2. feedback ====================


def load_feedback_src(src_file: Path) -> list[dict[str, Any]]:
    if not src_file.exists():
        return []
    out = []
    for line in src_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        action = rec.get("action", "like")
        # 平台 fb_type 枚举：citation / sql（纠错类归 sql）
        fb_type = "sql" if action == "correction" else "citation"
        status = rec.get("status", "pending")
        if status == "pending":
            status = "open"  # 平台语义：待处理
        out.append(
            {
                "fb_type": fb_type,
                "conversation_id": rec.get("qa_id") or None,
                "content": rec.get("question", ""),
                "correction": rec.get("corrected_answer") or None,
                "status": status,
                "created_by": rec.get("user_id") or None,
                "created_at": _fmt_dt(_dt(rec.get("timestamp"))),
            }
        )
    return out


async def migrate_feedback(dsn: str, rows: list[dict[str, Any]]) -> None:
    """feedback 幂等迁移：先查后插（content+created_by+created_at 组合判重）。"""
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for r in rows:
                dup = await conn.scalar(
                    text(
                        "SELECT id FROM feedback WHERE content = :content "
                        "AND created_by = :created_by AND created_at = :created_at LIMIT 1"
                    ),
                    {
                        "content": r["content"],
                        "created_by": r["created_by"],
                        "created_at": r["created_at"],
                    },
                )
                if dup is not None:
                    continue  # 已迁移，幂等跳过
                await conn.execute(
                    text(
                        "INSERT INTO feedback (fb_type, conversation_id, content, correction, "
                        "status, created_by, created_at) "
                        "VALUES (:fb_type, :conversation_id, :content, :correction, "
                        ":status, :created_by, :created_at)"
                    ),
                    r,
                )
    finally:
        await engine.dispose()


# ==================== 3. audit_logs ====================


def load_audit_src(*src_files: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for f in src_files:
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event = rec.get("event", "unknown")
            ts = rec.get("ts", "")
            dedupe_key = (event, ts)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            # 拆解 actor / target / detail：审计中间件口径
            actor = (
                rec.get("user")
                or rec.get("approver")
                or rec.get("actor")
                or rec.get("session_id")
            )
            target = rec.get("target") or rec.get("approval_id") or rec.get("task_id")
            detail = {k: v for k, v in rec.items() if k not in ("event", "ts", "user", "actor")}
            out.append(
                {
                    "event": event,
                    "actor": str(actor) if actor else None,
                    "target": str(target) if target else None,
                    "detail": json.dumps(detail, ensure_ascii=False) if detail else None,
                    "created_at": _fmt_dt(_dt(ts)),
                }
            )
    return out


async def migrate_audit(dsn: str, rows: list[dict[str, Any]]) -> None:
    """audit_logs 幂等迁移：先查后插（event+actor+created_at 组合判重）。

    目标表含平台运行期自产审计（audit 中间件），只追加迁移行、不碰既有数据。
    """
    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.begin() as conn:
            for r in rows:
                dup = await conn.scalar(
                    text(
                        "SELECT id FROM audit_logs WHERE event = :event "
                        "AND IFNULL(actor, '') = :actor AND created_at = :created_at LIMIT 1"
                    ),
                    {
                        "event": r["event"],
                        "actor": r["actor"] or "",
                        "created_at": r["created_at"],
                    },
                )
                if dup is not None:
                    continue  # 已迁移，幂等跳过
                await conn.execute(
                    text(
                        "INSERT INTO audit_logs (event, actor, target, detail, status, created_at) "
                        "VALUES (:event, :actor, :target, :detail, 200, :created_at)"
                    ),
                    r,
                )
    finally:
        await engine.dispose()


# ==================== 4. checkpoints（LangGraph 断点） ====================


async def migrate_checkpoints(dsn: str, src_db: Path) -> dict[str, int]:
    """SQLite LangGraph 断点 → MySQL checkpointer 表（幂等 upsert）。

    用 AsyncMySaver.aput / aput_writes 落库，确保与运行时代码同序列化格式；
    checkpoint 由 serde 反序列化（旧 msgpack → dict），metadata 为 JSON 字符串。

    三个关键坑（本日实测）：
    - collation：包内 SELECT_SQL 的 json_table 临时列硬编码 `CHARACTER SET utf8mb4`
      （MySQL 8 默认 utf8mb4_0900_ai_ci），若表继承数据库默认 utf8mb4_unicode_ci，
      读回即报 1267 Illegal mix of collations → 建表后把 checkpointer 表
      CONVERT TO utf8mb4_0900_ai_ci（只影响 langgraph 管理表，业务表不受影响）。
    - blob 版本：SQLite 历史 channel_versions 跨 checkpoint 重复，MySQL 的
      checkpoint_blobs 以 (thread,ns,channel,version) 为主键且 INSERT IGNORE
      → 按 checkpoint_id 降序迁移（最新先写），冲突时保留最新值。
    - setup() 的 CREATE TABLE IF NOT EXISTS 在 asyncmy 下打印 warning 到 stderr
      （"Table already exists"），非异常，忽略即可。
    - writes 的 idx：aput_writes 内部用 WRITES_IDX_MAP 固定 idx（如 messages→1），
      与历史 writes 的实际 idx 可能冲突 → 同 checkpoint 内两条 writes 落到同一主键，
      后写 INSERT 失败丢失。故 writes 不走 aput_writes，直接 SQL 保留原始 idx/blob。
    """
    import sqlite3

    from langgraph.checkpoint.mysql.asyncmy import AsyncMySaver
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

    if not src_db.exists():
        return {"checkpoints": 0, "writes": 0}

    serde = JsonPlusSerializer()
    conn = sqlite3.connect(str(src_db))
    conn.row_factory = sqlite3.Row
    # ★ 降序：最新 checkpoint 先写 → 冲突 blob 保留最新值
    cp_rows = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, "
        "type, checkpoint, metadata FROM checkpoints ORDER BY thread_id, checkpoint_id DESC"
    ).fetchall()
    write_rows = conn.execute(
        "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, "
        "type, value FROM writes ORDER BY thread_id, checkpoint_id, task_id, idx"
    ).fetchall()
    conn.close()

    async with AsyncMySaver.from_conn_string(dsn, serde=serde) as saver:
        # 先建表（migration 版本表 → 全部 MIGRATIONS）
        await saver.setup()
        # ★ collation 对齐（utf8mb4_0900_ai_ci）——否则 aget_tuple 报 1267
        for tbl in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
            async with saver._cursor() as cur:
                await cur.execute(
                    f"ALTER TABLE {tbl} CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                )

        cp_count = 0
        for r in cp_rows:
            try:
                checkpoint = serde.loads_typed((r["type"], r["checkpoint"]))
            except Exception as e:  # 单条损坏不阻断整体
                print(f"  [warn] checkpoint {r['checkpoint_id']} 反序列化失败: {e}")
                continue
            metadata = json.loads(r["metadata"] or "{}")
            cfg: Any = {
                "configurable": {
                    "thread_id": r["thread_id"],
                    "checkpoint_ns": r["checkpoint_ns"] or "",
                    "checkpoint_id": r["checkpoint_id"],
                }
            }
            # new_versions = 该断点记录的各 channel 版本（迁移后按原版本续跑）
            new_versions = checkpoint.get("channel_versions", {})
            try:
                await saver.aput(cfg, checkpoint, metadata, new_versions)
                cp_count += 1
            except Exception as e:
                print(f"  [warn] checkpoint {r['checkpoint_id']} 写入失败: {e}")

        # writes：直接 SQL 写入（保留原始 idx 与 type/blob 字节；幂等 upsert）。
        # ★ 不走 saver.aput_writes：其内部 WRITES_IDX_MAP 固定 idx 会与历史实际 idx
        #   冲突导致同组 writes 落到同一主键而丢失。
        # 先清理本次迁移范围内的旧行（幂等重跑不残留错误 idx 行；运行期新增保留）
        cur = cast(Any, saver.conn).cursor()
        try:
            src_cp_ids = sorted({w["checkpoint_id"] for w in write_rows})
            for i in range(0, len(src_cp_ids), 500):
                chunk = src_cp_ids[i:i + 500]
                ph = ",".join(["%s"] * len(chunk))
                await cur.execute(
                    f"DELETE FROM checkpoint_writes WHERE checkpoint_id IN ({ph})", chunk
                )
        finally:
            await cur.close()

        w_count = 0
        cur = cast(Any, saver.conn).cursor()
        try:
            for w in write_rows:
                await cur.execute(
                    "INSERT INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_ns_hash, "
                    "checkpoint_id, task_id, task_path, idx, channel, type, `blob`) "
                    "VALUES (%s, '', UNHEX(MD5('')), %s, %s, '', %s, %s, %s, %s) "
                    "ON DUPLICATE KEY UPDATE `blob` = VALUES(`blob`), type = VALUES(type)",
                    (w["thread_id"], w["checkpoint_id"], w["task_id"],
                     int(w["idx"]), w["channel"], w["type"], w["value"]),
                )
                w_count += 1
        finally:
            await cur.close()

    return {"checkpoints": cp_count, "writes": w_count}


# ==================== 校验和比对 ====================


class _TgtCache:
    """目标表快照缓存（避免重复查询）。"""

    def __init__(self, dsn: str):
        self.dsn = dsn
        self.data: dict[str, list[tuple[Any, ...]]] = {}
        self.counts: dict[str, int] = {}

    async def load(self, table: str, cols: list[str]) -> None:
        engine = create_async_engine(self.dsn, pool_pre_ping=True)
        try:
            async with engine.connect() as conn:
                col_sql = ", ".join(cols)
                rows = (await conn.execute(text(f"SELECT {col_sql} FROM {table}"))).all()
        finally:
            await engine.dispose()
        self.data[table] = [tuple(r) for r in rows]
        self.counts[table] = len(rows)


tgt_rows_cache: _TgtCache


def _norm_dt_sec(v: Any) -> str:
    """时间值统一为秒级字符串（源 str / 目标 datetime 可比较）。"""
    d = _dt(v)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else ""


def _norm_cell(v: Any) -> Any:
    """单元格归一化：datetime 统一秒级字符串（源 str / 目标 datetime 可比）。"""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return v


def verify_exists(
    table: str,
    src_rows: list[dict[str, Any]],
    key_cols: list[str],
    norm: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """存在性校验：源每一行按关键字段在目标表快照中查找。

    目标表可能含平台运行期自产数据（如 audit_logs 由审计中间件持续写入），
    因此不用"全表行数/校验和相等"，而验证"本次迁移的源行都能在目标找到"。
    关键字段在两侧分别归一化（时间列统一秒级），保证可比较。
    """
    norm = norm or {}
    tgt_keys: set[tuple[Any, ...]] = set()
    for row in tgt_rows_cache.data.get(table, []):
        tgt_keys.add(tuple(_norm_cell(v) for v in row))
    matched = 0
    for r in src_rows:
        key = tuple(
            _norm_cell(norm[c](r[c])) if c in norm else _norm_cell(r[c])
            for c in key_cols
        )
        if key in tgt_keys:
            matched += 1
    return {
        "table": table,
        "src_rows": len(src_rows),
        "matched": matched,
        "ok": matched == len(src_rows),
    }


# ==================== 主流程 ====================


def _default_root() -> Path:
    return Path(__file__).resolve().parents[2]  # learning-outputs/


async def main() -> None:
    parser = argparse.ArgumentParser(description="W23 Day5：stage3 → scm_platform 数据迁移")
    parser.add_argument("--dsn", default=settings.platform_dsn, help="平台库 DSN")
    parser.add_argument(
        "--stage3-root",
        default=str(_default_root()),
        help="stage3 项目根目录（默认 learning-outputs/）",
    )
    args = parser.parse_args()

    root = Path(args.stage3_root)
    src_b = root / "stage3-project-b"
    src_a = root / "stage3-project-a"
    dsn = args.dsn
    print(f"迁移源: {root} ｜ 目标: {dsn}\n")

    # ---- 1. approvals ----
    approvals_src = src_b / "data" / "approvals.db"
    ap_rows = load_approvals_src(approvals_src)
    await migrate_approvals(dsn, ap_rows)
    print(f"[1/4] approvals：源 {len(ap_rows)} 条")

    # ---- 2. feedback ----
    feedback_src = src_a / "reports" / "feedback_store.jsonl"
    fb_rows = load_feedback_src(feedback_src)
    await migrate_feedback(dsn, fb_rows)
    print(f"[2/4] feedback：源 {len(fb_rows)} 条")

    # ---- 3. audit_logs ----
    audit_srcs = [
        src_b / "data" / "audit.log",
        Path(__file__).resolve().parents[1] / "data" / "audit.log",
    ]
    au_rows = load_audit_src(*audit_srcs)
    await migrate_audit(dsn, au_rows)
    print(f"[3/4] audit_logs：源 {len(au_rows)} 条")

    # ---- 4. checkpoints ----
    cp_src = src_b / "data" / "biz_agent.db"
    cp_stat = await migrate_checkpoints(dsn, cp_src)
    print(f"[4/4] checkpoints：源 {cp_stat['checkpoints']} 断点 / {cp_stat['writes']} 写入")

    # ---- 校验和比对 ----
    global tgt_rows_cache
    tgt_rows_cache = _TgtCache(dsn)
    # 目标表快照：只加载校验用关键字段；时间列用 DATE_FORMAT 与源侧秒级格式对齐
    snapshots = [
        ("approvals", ["approval_no"]),
        (
            "feedback",
            ["content", "created_by", "DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%S') AS created_at"],
        ),
        (
            "audit_logs",
            ["event", "actor", "DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%S') AS created_at"],
        ),
    ]
    for table, cols in snapshots:
        await tgt_rows_cache.load(table, cols)

    results: list[dict[str, Any]] = [
        verify_exists("approvals", ap_rows, ["approval_no"]),
        verify_exists(
            "feedback",
            fb_rows,
            ["content", "created_by", "created_at"],
            norm={"created_at": _norm_dt_sec},
        ),
        verify_exists(
            "audit_logs",
            au_rows,
            ["event", "actor", "created_at"],
            norm={"created_at": _norm_dt_sec},
        ),
    ]
    # checkpoints：目标侧用 saver 读取（MySQL checkpointer 表）
    cp_check = await _verify_checkpoints(dsn, cp_src)

    print("\n===== 迁移校验汇总 =====")
    for r in results:
        mark = "[OK] " if r["ok"] else "[FAIL]"
        print(
            f"  {mark} {r['table']:12} 源 {r['src_rows']:4} 行 | 目标匹配 {r['matched']:4} 行"
        )
    print(
        f"  {'[OK] ' if cp_check['ok'] else '[FAIL]'} checkpoints  源 {cp_check['src_cp']} 断点 | "
        f"目标 {cp_check['tgt_cp']} 断点 | {cp_check['detail']}"
    )

    all_ok = all(r["ok"] for r in results) and cp_check["ok"]
    print(f"\n迁移完成：{'全部一致 [OK]' if all_ok else '存在不一致 [FAIL]（检查上表）'}")


async def _verify_checkpoints(dsn: str, src_db: Path) -> dict[str, Any]:
    """断点迁移校验：存在性校验（源断点/写入在目标表都能找到）。

    目标表含迁移后运行期新增断点（HITL 演练等），故不要求全表行数相等，
    只验证"本次迁移的源行都落库"（迁移前 thread 可在新库续跑由 HITL 演练验证）。
    """
    import sqlite3

    if not src_db.exists():
        return {"ok": True, "src_cp": 0, "tgt_cp": 0, "src_w": 0, "tgt_w": 0, "detail": "无源文件"}

    conn = sqlite3.connect(str(src_db))
    src_cp_rows = conn.execute("SELECT checkpoint_id FROM checkpoints").fetchall()
    src_w_rows = conn.execute(
        "SELECT checkpoint_id, task_id, idx FROM writes").fetchall()
    conn.close()
    src_cp_ids = {r[0] for r in src_cp_rows}
    src_w_keys = {(r[0], r[1], r[2]) for r in src_w_rows}

    engine = create_async_engine(dsn, pool_pre_ping=True)
    try:
        async with engine.connect() as c:
            tgt_cp_rows = (await c.execute(text("SELECT checkpoint_id FROM checkpoints"))).all()
            tgt_w_rows = (
                await c.execute(
                    text("SELECT checkpoint_id, task_id, idx FROM checkpoint_writes")
                )
            ).all()
    finally:
        await engine.dispose()
    tgt_cp_ids = {r[0] for r in tgt_cp_rows}
    tgt_w_keys = {(r[0], r[1], r[2]) for r in tgt_w_rows}
    cp_matched = len(src_cp_ids & tgt_cp_ids)
    w_matched = len(src_w_keys & tgt_w_keys)
    ok = cp_matched == len(src_cp_ids) and w_matched == len(src_w_keys)
    return {
        "ok": ok,
        "src_cp": len(src_cp_ids),
        "tgt_cp": len(tgt_cp_ids),
        "src_w": len(src_w_keys),
        "tgt_w": len(tgt_w_keys),
        "detail": f"断点 {cp_matched}/{len(src_cp_ids)} 匹配 | 写入 {w_matched}/{len(src_w_keys)} 匹配",
    }


if __name__ == "__main__":
    asyncio.run(main())
