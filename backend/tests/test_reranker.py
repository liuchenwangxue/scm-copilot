"""W27 Day5 覆盖率冲刺 I：重排器（reranker.py 0% → ≥85%）。

覆盖手册 Day5：
- RuleReranker：doc 去重（同文档只留最高分块）+ top_k 截断 + RRF 保序
- BGEReranker：加载失败 → 降级 RuleReranker（注入假模型类，不真下载 bge）；
  打分成功路径按相关性分降序 + doc 去重 + 稳定排序；打分异常降级
- get_reranker：force / 环境变量 LLM_RERANKER 选择
"""
import sys
import types

import pytest

from app.shared.rag.reranker import BGEReranker, RuleReranker, get_reranker


def _candidate(chunk_id: str, doc_id: str, score: float = 1.0, text: str = "条文内容") -> dict:
    return {
        "chunk_id": chunk_id,
        "doc_id": doc_id,
        "text": text,
        "fused_score": score,
        "source": "test",
    }


class TestRuleReranker:
    def test_doc_dedup_keeps_first(self):
        rr = RuleReranker()
        cands = [_candidate("c1", "doc-a"), _candidate("c2", "doc-a"), _candidate("c3", "doc-b")]
        out = rr.rerank("查询", cands, top_k=5)
        assert [c["chunk_id"] for c in out] == ["c1", "c3"], "同文档只保留首个"

    def test_top_k_truncates(self):
        rr = RuleReranker()
        cands = [_candidate(f"c{i}", f"doc-{i}") for i in range(8)]
        assert len(rr.rerank("查询", cands, top_k=3)) == 3

    def test_order_preserved_when_all_docs_unique(self):
        rr = RuleReranker()
        cands = [_candidate("c1", "d1"), _candidate("c2", "d2"), _candidate("c3", "d3")]
        assert [c["chunk_id"] for c in rr.rerank("q", cands)] == ["c1", "c2", "c3"]

    def test_empty_candidates(self):
        assert RuleReranker().rerank("q", []) == []


class TestBGERerankerDegrade:
    def test_load_failure_degrades_to_rule(self, monkeypatch):
        """模型加载失败（无网/下载失败）→ 自动降级 RuleReranker，name 诚实标注。"""
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)

        class _FailLoader:
            @staticmethod
            def from_pretrained(*a, **kw):
                raise OSError("connection error (simulated)")

        fake_trf = types.ModuleType("transformers")
        fake_trf.AutoModelForSequenceClassification = _FailLoader
        fake_trf.AutoTokenizer = _FailLoader
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setitem(sys.modules, "transformers", fake_trf)

        r = BGEReranker()
        cands = [_candidate("c1", "d1"), _candidate("c2", "d1"), _candidate("c3", "d2")]
        out = r.rerank("问题", cands, top_k=5)
        assert [c["chunk_id"] for c in out] == ["c1", "c3"], "降级后走 doc 去重"
        assert r.name == "bge-failed→rule"

    def test_score_failure_degrades_to_rule(self, monkeypatch):
        r = BGEReranker()
        r._model = object()  # 加载"成功"

        def _boom(self, query, texts):
            raise RuntimeError("inference failed (simulated)")

        monkeypatch.setattr(BGEReranker, "_score", _boom)
        cands = [_candidate("c1", "d1"), _candidate("c2", "d2")]
        assert [c["chunk_id"] for c in r.rerank("q", cands)] == ["c1", "c2"]

    def test_pending_name_before_load(self):
        assert BGEReranker().name == "bge(pending)"


class TestBGERerankerScore:
    def test_score_rank_dedup(self, monkeypatch):
        r = BGEReranker()
        r._model = object()
        monkeypatch.setattr(BGEReranker, "_score", lambda self, q, texts: [0.5, 0.9, 0.1])
        cands = [
            _candidate("c1", "d1"),
            _candidate("c2", "d2"),
            _candidate("c3", "d3"),
        ]
        out = r.rerank("q", cands, top_k=5)
        assert [c["chunk_id"] for c in out] == ["c2", "c1", "c3"], "按相关性分降序 + doc 去重"

    def test_score_top_k_truncates(self, monkeypatch):
        r = BGEReranker()
        r._model = object()
        monkeypatch.setattr(BGEReranker, "_score", lambda self, q, texts: [1.0, 0.8, 0.6, 0.4])
        cands = [_candidate(f"c{i}", f"d{i}") for i in range(4)]
        assert len(r.rerank("q", cands, top_k=2)) == 2

    def test_stable_sort_on_tie(self, monkeypatch):
        r = BGEReranker()
        r._model = object()
        monkeypatch.setattr(BGEReranker, "_score", lambda self, q, texts: [0.5, 0.5, 0.5])
        cands = [_candidate("c1", "d1"), _candidate("c2", "d2"), _candidate("c3", "d3")]
        assert [c["chunk_id"] for c in r.rerank("q", cands)] == ["c1", "c2", "c3"], "同分保序"

    def test_bge_name_after_load(self):
        r = BGEReranker()
        r._model = object()
        assert r.name == "bge(bge-reranker-base)"


class TestGetReranker:
    def test_force_rule(self):
        assert isinstance(get_reranker(force="rule"), RuleReranker)

    def test_force_bge(self):
        assert isinstance(get_reranker(force="bge"), BGEReranker)

    def test_env_rule(self, monkeypatch):
        monkeypatch.setenv("LLM_RERANKER", "rule")
        assert isinstance(get_reranker(), RuleReranker)

    def test_env_unknown_falls_back_to_rule(self, monkeypatch):
        monkeypatch.setenv("LLM_RERANKER", "unknown-xyz")
        assert isinstance(get_reranker(), RuleReranker)


class TestTokenizeCn:
    def test_tokenize_keeps_numbers_and_clauses(self):
        from app.shared.rag.reranker import _tokenize_cn

        words = _tokenize_cn("第3条规定采购金额超过100万元必须招标")
        assert any("100" in w for w in words), "数字应整块保留"
        assert any("3" in w for w in words), "条款号应整块保留"

    def test_tokenize_strips_stopwords(self):
        from app.shared.rag.reranker import _tokenize_cn

        words = _tokenize_cn("请问采购审批的流程是怎么规定的")
        assert "请问" not in words, "停用词应剔除"
        assert "流程" not in words, "业务性停用词（流程/管理）也应剔除"
        assert "采购" in words


class TestBGERerankerRealLoad:
    def test_load_success_cuda(self, monkeypatch):
        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: True)

        class _FakeAutoModel:
            def __init__(self):
                self._device = None

            @staticmethod
            def from_pretrained(name):
                return _FakeAutoModel()

            def to(self, device):
                self._device = device
                return self

            def eval(self):
                return self

        class _FakeAutoTokenizer:
            @staticmethod
            def from_pretrained(name):
                return object()

        fake_trf = types.ModuleType("transformers")
        fake_trf.AutoModelForSequenceClassification = _FakeAutoModel
        fake_trf.AutoTokenizer = _FakeAutoTokenizer
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setitem(sys.modules, "transformers", fake_trf)

        r = BGEReranker()
        assert r._load() is not None
        assert r._device == "cuda"
        assert r.name == "bge(bge-reranker-base)"

    def test_score_success_path(self, monkeypatch):
        import contextlib

        class _FakeTensor:
            shape = (1, 2)

            def __getitem__(self, i):
                return _FakeTensor()

        class _FakeModel:
            def __call__(self, **kw):
                return types.SimpleNamespace(logits=_FakeTensor())

        class _FakeTokenizer:
            def __call__(self, pairs, **kw):
                return {"input_ids": object()}

        class _FakeF:
            @staticmethod
            def sigmoid(x):
                return [0.9, 0.5, 0.1]

        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_torch.no_grad = contextlib.nullcontext
        fake_torch.nn = types.SimpleNamespace(functional=_FakeF())
        fake_trf = types.ModuleType("transformers")
        fake_trf.AutoModelForSequenceClassification = types.SimpleNamespace(
            from_pretrained=staticmethod(lambda name: _FakeModel())
        )
        fake_trf.AutoTokenizer = types.SimpleNamespace(
            from_pretrained=staticmethod(lambda name: _FakeTokenizer())
        )
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setitem(sys.modules, "transformers", fake_trf)

        r = BGEReranker()
        cands = [_candidate("c1", "d1"), _candidate("c2", "d2"), _candidate("c3", "d3")]
        out = r.rerank("q", cands, top_k=2)
        assert [c["chunk_id"] for c in out] == ["c1", "c2"], "按 sigmoid 相关性分降序 + doc 去重"

    def test_score_squeeze_branch(self, monkeypatch):
        """单输出 logits（shape[-1]==1）走 squeeze 分支。"""
        import contextlib

        class _Tensor:
            shape = (1, 1)

            def squeeze(self, dim=None):
                return self

        class _Model:
            def __call__(self, **kw):
                return types.SimpleNamespace(logits=_Tensor())

        class _Tokenizer:
            def __call__(self, pairs, **kw):
                return {}

        class _F:
            @staticmethod
            def sigmoid(x):
                return [0.8]

        fake_torch = types.ModuleType("torch")
        fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
        fake_torch.no_grad = contextlib.nullcontext
        fake_torch.nn = types.SimpleNamespace(functional=_F())
        fake_trf = types.ModuleType("transformers")
        fake_trf.AutoModelForSequenceClassification = types.SimpleNamespace(
            from_pretrained=staticmethod(lambda name: _Model())
        )
        fake_trf.AutoTokenizer = types.SimpleNamespace(
            from_pretrained=staticmethod(lambda name: _Tokenizer())
        )
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setitem(sys.modules, "transformers", fake_trf)

        r = BGEReranker()
        cands = [_candidate("c1", "d1")]
        assert r.rerank("q", cands) == [cands[0]]
