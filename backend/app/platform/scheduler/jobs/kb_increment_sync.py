"""★ kb_increment_sync：知识库增量同步（*/5min，W25 Day2，本周最重要任务）。

cron: */5 * * * *
作用（对照手册 Day2 上午）：
- 扫描 `config.KB_DOCS_DIR`（可指向 stage3-a/data/docs 复用 57 篇制度文档）：
  - 表无此 doc_id → 新文档入库
  - 文件 mtime > 表记录（严格 >，手册坑）或内容 hash 变化 → 重切块重嵌入
  - 表有但目录无 → 删除（Qdrant 按 payload `source_doc_id` 过滤删向量 + docs 表标 deleted）
- 幂等（面试题：uuid5 内容寻址 + last_sync_ts 水位）：
  - Qdrant point id = `uuid5(text)`（内容寻址）→ 重复 upsert 覆盖，零重复
  - 变更文档先按 `source_doc_id` 删旧向量再插入（增量拿不到 point id，手册坑）
  - `kb:sync:last_ts` 水位存 Redis，**任务成功后推进、失败不推进**（失败下轮重扫）

首轮初始化：docs 表为空 → 全量处理；collection 存在但无 `source_doc_id` payload
（stage3 旧格式）→ 自动重建，避免新旧 point id 并存。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.platform.models import DocMeta
from app.shared import config
from app.shared.rag.embedder import Embedder
from app.shared.rag.store import SCMStore
from app.shared.reliability.redis_client import RedisClient, get_redis_client

CRON = "*/5 * * * *"

# 内容寻址命名空间（uuid5 幂等键；固定值保证跨进程一致）
_NS = UUID("6ba7b811-9dad-11d1-80b4-00c04fd430c8")

MAX_CHUNK_CHARS = 800
_HEAD_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_SPLIT_RE = re.compile(r"[。；;]")

# 同步水位 key（Redis；成功推进，失败不推进——手册坑）
LAST_TS_KEY = "kb:sync:last_ts"

_TOPIC_MAP = {
    "PUR": "采购",
    "SUP": "供应商",
    "INV": "库存",
    "LOG": "物流",
    "QC": "质量",
    "FIN": "结算",
    "CMP": "合规",
    "ORG": "组织",
}


# ==================== 切块（与 stage3-a day3_chunk.py title 策略一致） ====================


def _emit(text: str, section: str, doc_id: str, idx: dict, out: list) -> None:
    out.append(
        {
            "chunk_id": f"{doc_id}#{idx['n']}",
            "doc_id": doc_id,
            "section_path": section,
            "text": text,
            "char_len": len(text),
        }
    )
    idx["n"] += 1


def _split_oversize(text: str, section: str, doc_id: str, idx: dict, out: list) -> None:
    """>800 字符的块：先按段落，再按句子边界尽量在 800 内断（保持 chunk 内容稳定，
    同一文档重切块时块边界可复现——幂等 upsert 的前提）。"""
    if len(text) <= MAX_CHUNK_CHARS:
        _emit(text, section, doc_id, idx, out)
        return
    parts = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    buf = ""
    for part in parts:
        while len(part) > MAX_CHUNK_CHARS:
            m = _SPLIT_RE.search(part, MAX_CHUNK_CHARS // 2)
            cut = m.start() + 1 if m else MAX_CHUNK_CHARS
            _emit(part[:cut], section, doc_id, idx, out)
            part = part[cut:].lstrip()
        if buf and len(buf) + len(part) > MAX_CHUNK_CHARS:
            _emit(buf, section, doc_id, idx, out)
            buf = part
        else:
            buf = (buf + "\n\n" + part) if buf else part
    if buf:
        _emit(buf, section, doc_id, idx, out)


def chunk_title(text: str, doc_id: str) -> list[dict]:
    """标题层级感知切块：按 #/##/### 边界切，块 >800 字符按段落/句子拆。"""
    out: list[dict] = []
    idx: dict[str, int] = {"n": 0}
    cur_section: str | None = None
    cur_text: list[str] = []
    for ln in text.splitlines():
        m = _HEAD_RE.match(ln)
        if m:
            if cur_section is not None and cur_text:
                _split_oversize("\n".join(cur_text).strip(), cur_section, doc_id, idx, out)
            cur_section = m.group(2).strip()
            cur_text = [ln]
        else:
            if cur_section is not None:
                cur_text.append(ln)
    if cur_section is not None and cur_text:
        _split_oversize("\n".join(cur_text).strip(), cur_section, doc_id, idx, out)
    return out


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def point_id_for(text: str) -> str:
    """★ 幂等键：point id = uuid5(内容)。同一文本块重复同步 → 同 id → upsert 覆盖零重复。"""
    return str(uuid5(_NS, text))


# ==================== 变更扫描（纯逻辑，可单测） ====================


def scan_changes(docs_dir: Path, rows: list[DocMeta], last_ts: float | None) -> dict[str, list]:
    """对比 docs 目录与 docs 表，得出三集合：new / changed / deleted。

    - new：表无此 doc_id（或曾标记 deleted 后文件回归）→ 入库
    - changed：mtime > 表记录（严格 >，手册坑）且 > 水位；或内容 hash 变化 → 重切块
    - deleted：表 active 但目录已无该文件 → 删向量 + 标记

    返回 {"new": [Path], "changed": [Path], "deleted": [doc_id]}。
    """
    by_id = {r.doc_id: r for r in rows}
    new, changed, deleted = [], [], []
    seen: set[str] = set()
    for md in sorted(docs_dir.glob("*.md")):
        doc_id = md.stem
        seen.add(doc_id)
        row = by_id.get(doc_id)
        if row is None or row.status == "deleted":
            new.append(md)  # 新文档 / 删除后回归：重新入库
            continue
        mtime_ts = md.stat().st_mtime
        # 水位内未变 → 快速跳过（水位成功后才推进，失败重扫）
        if last_ts is not None and mtime_ts <= last_ts:
            continue
        row_mtime = row.file_mtime.timestamp() if row.file_mtime else 0.0
        if mtime_ts > row_mtime or _sha256(md.read_text(encoding="utf-8")) != row.content_hash:
            changed.append(md)
    deleted = [d for d, row in by_id.items() if d not in seen and row.status == "active"]
    return {"new": new, "changed": changed, "deleted": deleted}


def _topic_of(doc_id: str) -> str:
    seg = doc_id.split("-")[1] if "-" in doc_id else ""
    return _TOPIC_MAP.get(seg, "未知")


def _title_of(doc_id: str) -> str:
    """doc_id 形如 `SCM-CMP-001_招标合规管理规范` → 取 `_` 后标题。"""
    return doc_id.split("_", 1)[1] if "_" in doc_id else doc_id


# ==================== Qdrant 写入/删除（幂等） ====================


def _ensure_collection(store: SCMStore, dim: int) -> str:
    """确保 collection 存在；检测 stage3 旧格式（payload 无 source_doc_id）→ 重建。

    旧 stage3 数据 point id 是数字偏移，无法被 source_doc_id 过滤删除，与其新旧并存
    不如首轮重建（57 篇文档切块嵌入一次约几分钟，仅发生一次）。
    """
    try:
        store.client.get_collection(store.collection)
    except Exception:  # noqa: BLE001  # collection 不存在
        store.create_collection(dim=dim, overwrite=False)
        return "created"
    try:
        pts = store.client.scroll(store.collection, limit=1, with_payload=True)[0]
    except Exception:  # noqa: BLE001
        pts = []
    if pts and "source_doc_id" not in (pts[0].payload or {}):
        store.create_collection(dim=dim, overwrite=True)
        return "rebuilt"
    return "ok"


def _delete_doc_vectors(store: SCMStore, doc_id: str) -> int:
    """按 payload `source_doc_id` 过滤删除（手册坑：不按 point id，增量拿不到 id）。"""
    from qdrant_client.models import (
        FieldCondition,
        Filter,
        FilterSelector,
        MatchValue,
    )

    selector = FilterSelector(
        filter=Filter(must=[FieldCondition(key="source_doc_id", match=MatchValue(value=doc_id))])
    )
    deleted = store.client.delete(
        collection_name=store.collection,
        points_selector=selector,
        wait=True,
    )
    return int(getattr(deleted, "status", deleted) is not None)


def _upsert_chunks(store: SCMStore, embedder: Embedder, chunks: list[dict]) -> int:
    """批量嵌入 + 幂等 upsert：point id = uuid5(text)，payload 带 source_doc_id。"""
    from qdrant_client.models import PointStruct

    texts = [c["text"] for c in chunks]
    vectors = embedder.batch_embed(texts, batch_size=64)
    points = [
        PointStruct(
            id=point_id_for(c["text"]),
            vector=vectors[i].tolist(),
            payload={
                "chunk_id": c["chunk_id"],
                "doc_id": c["doc_id"],
                "source_doc_id": c["doc_id"],  # ★ 删除/孤儿判定过滤键
                "section_path": c.get("section_path", ""),
                "topic": _topic_of(c["doc_id"]),
                "text": c["text"],
            },
        )
        for i, c in enumerate(chunks)
    ]
    for i in range(0, len(points), 100):
        store.client.upsert(
            collection_name=store.collection,
            points=points[i : i + 100],
            wait=True,
        )
    return len(points)


# ==================== 单文档入库 / 删除 ====================


async def _index_doc(
    session_factory: async_sessionmaker[AsyncSession],
    store: SCMStore,
    embedder: Embedder,
    md: Path,
) -> dict:
    """新/变更文档：重切块 → 嵌入 → 幂等 upsert → 更新 docs 表登记。"""
    text = md.read_text(encoding="utf-8")
    doc_id = md.stem
    chunks = chunk_title(text, doc_id)
    # 变更文档先删旧向量（增量场景拿不到旧 point id，按 source_doc_id 过滤删）
    _delete_doc_vectors(store, doc_id)
    n = _upsert_chunks(store, embedder, chunks)
    content_hash = _sha256(text)
    mtime_dt = _mtime_to_dt(md)
    async with session_factory() as session:
        row = await session.scalar(select(DocMeta).where(DocMeta.doc_id == doc_id))
        if row is None:
            session.add(
                DocMeta(
                    doc_id=doc_id,
                    file=md.name,
                    title=_title_of(doc_id),
                    topic=_topic_of(doc_id),
                    file_mtime=mtime_dt,
                    content_hash=content_hash,
                    chunk_count=n,
                    status="active",
                )
            )
        else:
            row.file = md.name
            row.title = _title_of(doc_id)
            row.topic = _topic_of(doc_id)
            row.file_mtime = mtime_dt
            row.content_hash = content_hash
            row.chunk_count = n
            row.status = "active"
        await session.commit()
    return {"doc_id": doc_id, "chunks": n, "action": "indexed"}


async def _delete_doc(
    session_factory: async_sessionmaker[AsyncSession],
    store: SCMStore,
    doc_id: str,
) -> dict:
    """删除文档：Qdrant 删向量 + docs 表标记 deleted（保留记录供审计/孤儿判定）。"""
    _delete_doc_vectors(store, doc_id)
    async with session_factory() as session:
        row = await session.scalar(select(DocMeta).where(DocMeta.doc_id == doc_id))
        if row is not None:
            row.status = "deleted"
            row.chunk_count = 0
            await session.commit()
    return {"doc_id": doc_id, "action": "deleted"}


def _mtime_to_dt(md: Path):
    from datetime import datetime

    return datetime.fromtimestamp(md.stat().st_mtime)


# ==================== 任务入口 ====================


async def _sync(
    session_factory: async_sessionmaker[AsyncSession],
    store: SCMStore,
    embedder: Embedder,
    rc: RedisClient,
    docs_dir: Path,
    scan_ts: float | None = None,
) -> dict:
    """增量同步主流程（可注入依赖，单测不碰真实 Qdrant/模型/Redis）。"""
    import time

    if not docs_dir.is_dir():
        return {
            "job": "kb_increment_sync",
            "status": "degraded",
            "error": f"docs dir not found: {docs_dir}",
        }

    scan_ts = scan_ts if scan_ts is not None else time.time()

    from sqlalchemy import select

    async with session_factory() as session:
        rows = list((await session.scalars(select(DocMeta))).all())

    # 首轮：collection 存在但为 stage3 旧格式 → 重建（一次性成本）
    _ensure_collection(store, embedder.dim)

    raw_last = rc.get(LAST_TS_KEY)
    last_ts = float(raw_last) if raw_last else None
    changes = scan_changes(docs_dir, rows, last_ts)

    indexed = []
    for md in changes["new"] + changes["changed"]:
        indexed.append(await _index_doc(session_factory, store, embedder, md))
    deleted = []
    for doc_id in changes["deleted"]:
        deleted.append(await _delete_doc(session_factory, store, doc_id))

    # ★ 水位成功后才推进（失败不推进——下轮重扫；scan_ts 取扫描开始时刻，
    #   保证扫描期间改动的文件 mtime > 水位，下轮继续处理不遗漏）
    rc.set(LAST_TS_KEY, str(scan_ts))

    return {
        "job": "kb_increment_sync",
        "status": "success",
        "new": len(changes["new"]),
        "changed": len(changes["changed"]),
        "deleted": len(changes["deleted"]),
        "indexed": indexed,
        "deleted_docs": deleted,
        "docs_total": len(rows) + len(changes["new"]) - len(changes["deleted"]),
    }


async def run(
    store: SCMStore | None = None,
    embedder: Embedder | None = None,
    rc: RedisClient | None = None,
    docs_dir: Path | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> dict:
    """调度器入口（无参契约）。单测可注入依赖。

    session_factory 从 scheduler 运行时上下文取（延迟 import 防循环引用）。
    """
    from app.platform.scheduler import _runtime

    sf = session_factory or _runtime.session_factory  # ★ W27-D6 B10：RuntimeContext 字段
    if sf is None:
        return {
            "job": "kb_increment_sync",
            "status": "degraded",
            "error": "scheduler runtime not initialized",
        }

    store = store or SCMStore()
    embedder = embedder or Embedder()
    rc = rc or get_redis_client()
    docs_dir = docs_dir or config.KB_DOCS_DIR
    try:
        return await _sync(sf, store, embedder, rc, docs_dir)
    except Exception as e:  # noqa: BLE001  # 任务失败由 scheduler 层记 failed
        return {"job": "kb_increment_sync", "status": "failed", "error": str(e)}


# 类型引用别名（模块级可导入，mypy 友好）
AsyncSessionFactory = async_sessionmaker[AsyncSession]
