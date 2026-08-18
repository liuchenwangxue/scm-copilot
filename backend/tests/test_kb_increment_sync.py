"""W25 Day2 kb_increment_sync 测试：切块/幂等键/变更扫描（纯逻辑）+ 全流程（integration）。

覆盖手册 Day2 验收：
- 幂等设计：uuid5 内容寻址（同文本同 id）+ last_sync_ts 水位（成功后推进/失败不推进）
- 变更检测：mtime 严格 `>`（避免边界重复）；新文档/变更/删除三集合判定
- 删除文档：Qdrant 按 source_doc_id 过滤删向量 + docs 表标记 deleted
"""

import os
from datetime import datetime

import numpy as np
import pytest

from app.platform.models import DocMeta
from app.platform.scheduler.jobs.kb_increment_sync import (
    _sha256,
    chunk_title,
    point_id_for,
    scan_changes,
)

pytestmark = pytest.mark.integration

# 固定时间戳（避免 flaky：mtime 精度 / 与"现在"的边界）
T0, T1, T2 = 1_700_000_000.0, 1_700_000_200.0, 1_700_000_400.0


# ==================== 切块（与 stage3-a day3_chunk.py 对齐） ====================


def test_chunk_title_splits_by_heading():
    text = "# 第一章\n\n第1条 内容。\n\n## 1.1 小节\n\n第2条 内容。\n"
    chunks = chunk_title(text, "SCM-CMP-001_招标合规")
    # 标题行与其下内容合并为一个块（与 stage3 day3_chunk title 策略一致）
    assert len(chunks) == 2
    assert chunks[0]["section_path"] == "第一章"
    assert chunks[0]["doc_id"] == "SCM-CMP-001_招标合规"
    assert chunks[0]["chunk_id"] == "SCM-CMP-001_招标合规#0"
    assert chunks[1]["section_path"] == "1.1 小节"
    # 全部为纯文本块（无空块/未归属文本丢失）
    assert all(c["text"].strip() for c in chunks)


def test_chunk_title_splits_oversize():
    text = "# 长文\n\n" + "第%s条 内容。" % ("测试" * 30 + "，" * 5) * 40
    chunks = chunk_title(text, "SCM-PUR-001_长文")
    assert all(c["char_len"] <= 800 for c in chunks)
    assert len(chunks) > 1


# ==================== 幂等键（uuid5 内容寻址） ====================


def test_point_id_content_addressed_idempotent():
    a = point_id_for("同一段文本")
    b = point_id_for("同一段文本")
    c = point_id_for("不同文本")
    assert a == b
    assert a != c
    # uuid 格式
    import uuid

    uuid.UUID(a)


# ==================== 变更扫描（纯逻辑，无 DB/Qdrant） ====================


def _mk_row(doc_id: str, mtime: float, content: str, status: str = "active") -> DocMeta:
    return DocMeta(
        doc_id=doc_id,
        file=f"{doc_id}.md",
        file_mtime=datetime.fromtimestamp(mtime),
        content_hash=_sha256(content),
        chunk_count=1,
        status=status,
    )


def _write(tmp, name: str, content: str, mtime: float):
    p = tmp / name
    p.write_text(content, encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def test_scan_changes_new_changed_deleted(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    content_a = "# 招标\n第1条 内容。"
    _write(docs, "SCM-CMP-001_招标合规.md", content_a, T0)
    rows = [_mk_row("SCM-CMP-001_招标合规", T0, content_a)]
    # 新文档 b + 目录中已删除的 c
    _write(docs, "SCM-PUR-001_采购.md", "# 采购\n第1条 内容。", T1)
    rows.append(_mk_row("SCM-LOG-001_物流", T0, "旧内容"))

    changes = scan_changes(docs, rows, last_ts=None)
    assert [p.name for p in changes["new"]] == ["SCM-PUR-001_采购.md"]
    assert changes["changed"] == []
    assert changes["deleted"] == ["SCM-LOG-001_物流"]


def test_scan_changes_mtime_strict_greater(tmp_path):
    """mtime 用 > 不用 >=：等于表记录 mtime 不算变更（避免边界重复）。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    content = "# 招标\n第1条 内容。"
    p = _write(docs, "SCM-CMP-001_招标合规.md", content, T0)
    rows = [_mk_row("SCM-CMP-001_招标合规", T0, content)]

    changes = scan_changes(docs, rows, last_ts=None)
    assert changes["new"] == []
    assert changes["changed"] == []  # mtime == 表记录 → 不算变更

    # mtime 前进（> T0）→ 变更
    os.utime(p, (T1, T1))
    changes = scan_changes(docs, rows, last_ts=None)
    assert changes["changed"] == [p]


def test_scan_changes_watermark_skip_after_success(tmp_path):
    """水位 last_ts：文件 mtime <= 水位 → 跳过（失败不推进水位的反面：成功推进后不重扫）。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    content = "# 招标\n第1条 内容。"
    _write(docs, "SCM-CMP-001_招标合规.md", content, T1)
    rows = [_mk_row("SCM-CMP-001_招标合规", T1, content)]

    changes = scan_changes(docs, rows, last_ts=T2)  # 水位已越过文件 mtime
    assert changes["new"] == []
    assert changes["changed"] == []


def test_scan_changes_deleted_doc_returns_as_new(tmp_path):
    """删除后文件回归（表 status=deleted）→ 视为新文档重新入库。"""
    docs = tmp_path / "docs"
    docs.mkdir()
    content = "# 回归\n内容。"
    p = _write(docs, "SCM-CMP-001_招标合规.md", content, T1)
    rows = [_mk_row("SCM-CMP-001_招标合规", T0, "旧", status="deleted")]
    changes = scan_changes(docs, rows, last_ts=None)
    assert changes["new"] == [p]


# ==================== 全流程（integration：MySQL + Fake Qdrant/Embedding/Redis） ====================


class FakeStore:
    """内存 Qdrant：只实现增量同步用到的原语。"""

    def __init__(self):
        self.collection = "scm_kb_v1"
        self.points: list[dict] = []
        self.delete_calls: list[str] = []

    def create_collection(self, dim: int, overwrite: bool = False) -> dict:
        return {"points_count": len(self.points)}

    class _Client:
        def __init__(self, store: "FakeStore"):
            self.store = store

        def get_collection(self, name: str) -> dict:
            return {"points_count": len(self.store.points)}

        def scroll(
            self, collection_name, limit, offset=None, with_payload=True, with_vectors=False
        ):
            return [], None

        def upsert(self, collection_name, points, wait=True):
            self.store.points.extend(points)
            return None

        def delete(self, collection_name, points_selector, wait=True):
            # 简化：记录被删除过滤的 source_doc_id（真实实现按 payload 过滤）
            from qdrant_client.models import FieldCondition, MatchValue

            condition = points_selector.filter.must[0]
            if isinstance(condition, FieldCondition) and isinstance(condition.match, MatchValue):
                self.store.delete_calls.append(condition.match.value)
            self.store.points = [
                p
                for p in self.store.points
                if (p.payload or {}).get("source_doc_id") != getattr(condition.match, "value", None)
            ]
            return type("R", (), {"status": True})()

    @property
    def client(self):
        return self._Client(self)


class FakeEmbedder:
    dim = 512

    def batch_embed(self, texts: list[str], batch_size: int = 64):
        return np.zeros((len(texts), self.dim), dtype=np.float32)


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.available = True

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str, ex=None) -> bool:
        self.store[key] = value
        return True

    def set_nx(self, key: str, value: str, ex=None) -> bool:
        if key in self.store:
            return False
        self.store[key] = value
        return True

    def delete_if_equals(self, key: str, expected: str) -> bool:
        if self.store.get(key) == expected:
            del self.store[key]
            return True
        return False


@pytest.fixture
async def factory():
    """MySQL session_factory（docs 表由 alembic/建表确保存在）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.platform.settings import settings

    engine = create_async_engine(settings.platform_dsn)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def clean_docs(factory):
    """docs 表隔离：快照运行前状态，结束后恢复（删新增测试记录 + 还原被改状态）。

    ★ 不能用 `DELETE FROM docs` 全清——正式知识库记录（stage3 同步的 57 篇）会被误删。
      全流程测试只用测试 doc_id，故按快照收敛即可保证互不污染。
    """
    from sqlalchemy import text

    async with factory() as s:
        before = {
            r.doc_id: r.status
            for r in (await s.execute(text("SELECT doc_id, status FROM docs"))).all()
        }
    yield
    async with factory() as s:
        # 删除运行期间新增的记录（非快照 doc_id）
        if before:
            ids = list(before)
            placeholders = ",".join(f":d{i}" for i in range(len(ids)))
            await s.execute(
                text(f"DELETE FROM docs WHERE doc_id NOT IN ({placeholders})"),
                {f"d{i}": d for i, d in enumerate(ids)},
            )
        else:
            await s.execute(text("DELETE FROM docs"))
        # 还原快照内 doc_id 的原状态（smoke/测试可能标 deleted）
        for doc_id, status in before.items():
            await s.execute(
                text("UPDATE docs SET status = :st WHERE doc_id = :d"),
                {"st": status, "d": doc_id},
            )
        await s.commit()


@pytest.mark.asyncio
async def test_sync_full_flow(tmp_path, factory, clean_docs):
    """全流程：首轮全量 → 改文档变更 → 删文档同步清向量（核心验收）。"""
    from app.platform.scheduler.jobs.kb_increment_sync import (
        LAST_TS_KEY,
        _sync,
    )

    docs = tmp_path / "docs"
    docs.mkdir()
    content_a = (
        "# 招标合规\n\n第1条 采购金额超过100万必须招标。\n\n## 1.1 例外\n\n第2条 紧急采购除外。"
    )
    content_b = "# 库存管理\n\n第1条 每周盘点一次。"
    _write(docs, "SCM-CMP-001_招标合规.md", content_a, T0)
    _write(docs, "SCM-INV-001_库存.md", content_b, T1)

    store = FakeStore()
    rc = FakeRedis()

    # ---- 首轮全量 ----
    r1 = await _sync(factory, store, FakeEmbedder(), rc, docs, scan_ts=T2)
    assert r1["status"] == "success"
    assert r1["new"] == 2
    # 幂等 upsert：point id = uuid5(text)（两次全量不重复）
    first_count = len(store.points)
    r1b = await _sync(factory, store, FakeEmbedder(), rc, docs, scan_ts=T2)
    assert r1b["new"] == 0 and r1b["changed"] == 0
    assert len(store.points) == first_count, "重复同步不应产生重复向量（uuid5 覆盖）"
    # 水位推进
    assert rc.get(LAST_TS_KEY) == str(T2)

    # ---- 变更：改一段 → 重切块重嵌入（先删后插） ----
    T3 = T2 + 100.0
    p_a = docs / "SCM-CMP-001_招标合规.md"
    content_a2 = content_a.replace("100万", "200万")
    p_a.write_text(content_a2, encoding="utf-8")
    os.utime(p_a, (T3, T3))
    before = len(store.points)
    r2 = await _sync(factory, store, FakeEmbedder(), rc, docs, scan_ts=T3)
    assert r2["changed"] == 1
    # 变更文档先删旧向量再插新 → 总点数不变（块数相同）
    assert "SCM-CMP-001_招标合规" in store.delete_calls
    assert len(store.points) == before

    # ---- 删除：文件消失 → 同步清向量 + 标 deleted ----
    T4 = T3 + 100.0
    p_b = docs / "SCM-INV-001_库存.md"
    p_b.unlink()
    r3 = await _sync(factory, store, FakeEmbedder(), rc, docs, scan_ts=T4)
    assert r3["deleted"] == 1
    assert "SCM-INV-001_库存" in store.delete_calls
    # docs 表标记 deleted
    from sqlalchemy import select

    async with factory() as s:
        row = await s.scalar(select(DocMeta).where(DocMeta.doc_id == "SCM-INV-001_库存"))
        assert row is not None and row.status == "deleted" and row.chunk_count == 0
