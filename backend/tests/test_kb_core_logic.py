"""kb 域轻量单测（W23 Day4 由 stage3-a `tests/test_core_logic.py` 迁移）。

平台化后调整：
- import 从 `backend.xxx` 改为 `app.shared.xxx` / `app.domains.kb.xxx`
- JWT/RBAC 测试移除——认证/权限已由平台 `test_auth.py` / `test_rbac.py` 覆盖（W23 Day3）

覆盖纯逻辑（CI 可跑——不依赖模型/网络/Qdrant）：
- parser：registry 扩展名路由 + 坏文件兜底（不读真 PDF/Word）
- metrics：core_of / answer_in_top5 / citation_accuracy
- query_rewriter：RRF 融合 + mock 退化
"""
import asyncio

# ==================== parser（registry 路由 + 坏文件兜底） ====================

def test_parser_supported_ext():
    from app.shared.rag.parser import SUPPORTED_EXT, doc_id_from_filename
    assert ".md" in SUPPORTED_EXT and ".pdf" in SUPPORTED_EXT and ".docx" in SUPPORTED_EXT
    assert doc_id_from_filename("SCM-PUR-001_x.pdf") == "SCM-PUR-001_x"


def test_parser_bad_file_skip(tmp_path):
    """坏文件兜底：非 PDF 内容 → ok=False + error，不抛异常。"""
    from app.shared.rag.parser import parse_document
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"%PDF not really")
    r = parse_document(bad)
    assert r["ok"] is False and r["error"]


def test_parser_unsupported_ext(tmp_path):
    from app.shared.rag.parser import parse_document
    bad = tmp_path / "x.xyz"
    bad.write_text("hello")
    r = parse_document(bad)
    assert r["ok"] is False and "不支持" in r["error"]


# ==================== metrics（检索指标） ====================

def test_core_of_strips_citation_prefix():
    from app.domains.kb.eval.metrics import core_of
    assert core_of("按《SCM-PUR-001》第 37 条，逾期未执行的申请自动失效") == "逾期未执行的申请自动失效"
    assert core_of("60 个自然日") == "60 个自然日"


def test_answer_in_top5():
    from app.domains.kb.eval.metrics import answer_in_top5
    top5 = "采购申请的有效期为自批准之日起 60 个自然日，逾期未执行的申请自动失效"
    assert answer_in_top5(top5, "采购申请的有效期为自批准之日起 60 个自然日")
    assert not answer_in_top5(top5, "完全不相关的内容片段")


def test_citation_accuracy_partial():
    from app.domains.kb.eval.metrics import citation_accuracy
    golden = {"A", "B"}
    assert citation_accuracy(["A"], golden) == 0.5
    assert citation_accuracy(["A", "B", "C"], golden) == 1.0
    assert citation_accuracy([], golden) == 0.0


# ==================== query_rewriter（RRF + mock 退化） ====================

def test_rrf_fuse_docs():
    from app.shared.rag.query_rewriter import rrf_fuse_docs
    docs = rrf_fuse_docs([["a", "b", "c"], ["b", "a", "d"]], top_k=5)
    # 两路都出现在高位：a/b 靠前；d 只一路且 rank 3
    assert docs[0] in ("a", "b")
    assert "d" in docs


def test_rewriter_mock_degrade():
    """mock provider 下三方案退化为规则（rewrite/hyde=原查询，multi_query=[原查询]）。"""
    from app.shared.llm import get_provider
    from app.shared.rag.query_rewriter import QueryRewriter
    rw = QueryRewriter(provider=get_provider("mock"))
    assert not rw.is_llm
    assert asyncio.run(rw.rewrite("测试问题")) == "测试问题"
    assert asyncio.run(rw.hyde("测试问题")) == "测试问题"
    assert asyncio.run(rw.multi_query("测试问题")) == ["测试问题"]
