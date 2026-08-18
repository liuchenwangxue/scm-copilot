"""kb_increment_sync 真实环境验收脚本（★ W25 Day2 验收：改文档 ≤5min 可检索）。

验证流程（对照手册 Day2 上午）：
1. 首次全量：临时 docs 目录 2 篇文档 → 同步 → docs 表登记 + Qdrant 向量可检索
2. 改文档：改一段内容 → 重新同步 → Qdrant 能检索到新内容（旧内容检索不到）
3. 删文档：删除文件 → 重新同步 → Qdrant 按 source_doc_id 清向量（检索不到）
4. 水位：成功后推进、重复同步零增量（uuid5 内容寻址幂等）

隔离性：用独立 collection `scm_kb_smoke_w25`（不碰正式 scm_kb_v1）；测试文档
doc_id 带 `测试` 后缀便于清理 docs 表记录。

用法:
    cd f:/code/agent/learning-outputs/scm-copilot
    .venv/Scripts/python.exe -X utf8 scripts/kb_sync_smoke.py
"""

import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.platform.models import DocMeta
from app.platform.scheduler.jobs.kb_increment_sync import LAST_TS_KEY, _sync
from app.platform.settings import settings
from app.shared.rag.embedder import Embedder
from app.shared.rag.store import SCMStore
from app.shared.reliability.redis_client import get_redis_client

COLLECTION = "scm_kb_smoke_w25"
DOC_A = "SCM-CMP-001_测试合规.md"
DOC_B = "SCM-INV-001_测试库存.md"


def _wait_mtime():
    time.sleep(1.2)  # mtime 秒级精度：保证连续写入的 mtime 严格递增


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="kb_smoke_"))
    docs = tmp / "docs"
    docs.mkdir()
    engine = create_async_engine(settings.platform_dsn)
    sf = async_sessionmaker(engine, expire_on_commit=False)
    store = SCMStore(collection=COLLECTION)
    rc = get_redis_client()
    embedder = Embedder()

    # ★ 状态保护：快照 docs 表现有记录（smoke 用临时目录会把不在目录的
    #   正式文档误标 deleted——运行后按快照恢复，避免污染正式登记）
    snapshot: dict[str, str] = {}
    test_doc_ids = {DOC_A.removesuffix(".md"), DOC_B.removesuffix(".md")}

    try:
        from sqlalchemy import select

        async with sf() as s:
            snapshot = {r.doc_id: r.status for r in (await s.scalars(select(DocMeta))).all()}

        # ============ 1) 首次全量 ============
        content_a = (
            "# 招标合规\n\n第1条 采购金额超过100万必须招标。\n\n## 1.1 例外\n\n第2条 紧急采购除外。"
        )
        content_b = "# 库存管理\n\n第1条 每周盘点一次。\n\n第2条 库存差异超过5%上报。"
        (docs / DOC_A).write_text(content_a, encoding="utf-8")
        (docs / DOC_B).write_text(content_b, encoding="utf-8")
        _wait_mtime()

        print("== [1] 首次全量同步 ==")
        r1 = await _sync(sf, store, embedder, rc, docs)
        print(
            f"    new={r1['new']} changed={r1['changed']} deleted={r1['deleted']} -> {r1['status']}"
        )
        assert r1["new"] == 2 and r1["status"] == "success"

        # 检索验证：招标金额标准
        hits = store.query(embedder.embed_query("采购金额超过多少必须招标").tolist(), top_k=5)
        doc_ids = {h["doc_id"] for h in hits}
        print(f"    检索'招标金额标准' 命中 doc_ids={doc_ids}")
        assert any(DOC_A.removesuffix(".md") in d for d in doc_ids), "首次入库后应可检索到文档A"

        # 幂等：重复同步零增量
        r1b = await _sync(sf, store, embedder, rc, docs)
        assert r1b["new"] == 0 and r1b["changed"] == 0, f"重复同步应零增量: {r1b}"
        print("    重复同步零增量（uuid5 幂等） OK")

        # ============ 2) 改文档 → 可检索新内容 ============
        print("== [2] 改文档（100万 → 200万）重同步 ==")
        (docs / DOC_A).write_text(content_a.replace("100万", "200万"), encoding="utf-8")
        os.utime(docs / DOC_A, (time.time() + 1.5, time.time() + 1.5))
        _wait_mtime()
        r2 = await _sync(sf, store, embedder, rc, docs)
        print(f"    changed={r2['changed']} -> {r2['status']}")
        assert r2["changed"] == 1

        hits2 = store.query(embedder.embed_query("采购金额超过200万必须招标").tolist(), top_k=5)
        texts = [h["text"] for h in hits2]
        has_new = any("200万" in t for t in texts)
        has_old = any("100万" in t for t in texts)
        print(f"    检索'200万' 新内容命中={has_new} 旧内容残留={has_old}")
        assert has_new and not has_old, "改文档后应检索到新内容、旧内容应被清理"

        # ============ 3) 删文档 → 向量同步消失 ============
        print("== [3] 删文档（库存）→ 向量同步清空 ==")
        (docs / DOC_B).unlink()
        os.utime(docs / DOC_A, (time.time() + 1.5, time.time() + 1.5))  # 触发水位前进
        _wait_mtime()
        r3 = await _sync(sf, store, embedder, rc, docs)
        print(f"    deleted={r3['deleted']} -> {r3['status']}")
        assert r3["deleted"] == 1

        hits3 = store.query(embedder.embed_query("库存盘点频率").tolist(), top_k=5)
        doc_b_id = DOC_B.removesuffix(".md")
        gone = all(h["doc_id"] != doc_b_id for h in hits3)
        print(f"    检索'库存盘点' 命中 doc_ids={[h['doc_id'] for h in hits3]}")
        assert gone, "删除文档后向量应同步消失"

        print("\n★ 全部通过：首次全量 / 幂等 / 改文档可检索 / 删文档清向量")
        return 0
    finally:
        # 清理：测试 collection + docs 表（删测试记录、恢复快照状态）+ 临时目录
        try:
            store.client.delete_collection(COLLECTION)
            print(f"    已清理 collection {COLLECTION}")
        except Exception as e:  # noqa: BLE001
            print(f"    collection 清理跳过: {e}")
        async with sf() as s:
            await s.execute(text("DELETE FROM docs WHERE doc_id LIKE 'SCM-%_测试%'"))
            # 恢复快照：smoke 期间被误标 deleted 的正式文档还原为原状态
            for doc_id, status in snapshot.items():
                if doc_id in test_doc_ids:
                    continue  # 测试文档本身要删
                await s.execute(
                    text("UPDATE docs SET status = :st WHERE doc_id = :d"),
                    {"st": status, "d": doc_id},
                )
            await s.commit()
        rc.delete(LAST_TS_KEY)
        await engine.dispose()
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
