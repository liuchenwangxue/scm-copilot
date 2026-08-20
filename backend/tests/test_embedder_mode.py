"""★ W28-D1 容器口径统一（C1）：Embedder mock/降级行为单测。

覆盖（不真下载 bge 模型，模型路径留容器验证）：
- mock 模式：SCM_EMBEDDER=mock → 512 维归一化确定性向量 / 同输入同输出 / status=mock
- real 加载失败：注入 _load 抛错 → 自动降级 mock_degraded + 记录 load_error
- dim / batch_embed 契约与 real 一致（维度 512）
- model_status 注册表：record/snapshot/probe_if_pending 幂等
"""

import numpy as np

from app.shared.rag import model_status
from app.shared.rag.embedder import Embedder
from app.shared.rag.model_status import (
    probe_if_pending,
    record_embedder,
    record_reranker,
    snapshot,
)


class TestMockMode:
    def test_mock_status_and_dim(self):
        e = Embedder(mode="mock")
        assert e.status_name() == "mock"
        assert e.dim == 512

    def test_mock_vector_deterministic_and_normalized(self):
        e = Embedder(mode="mock")
        v1 = e.embed_query("采购审批要经过哪几级")
        v2 = e.embed_query("采购审批要经过哪几级")
        assert v1.shape == (512,)
        np.testing.assert_array_equal(v1, v2)  # 同输入同输出（复现性）
        norm = float(np.linalg.norm(v1))
        assert abs(norm - 1.0) < 1e-5  # L2 归一化（Qdrant 余弦口径一致）

    def test_mock_different_input_diff_vector(self):
        e = Embedder(mode="mock")
        va = e.embed_query("采购审批要经过哪几级")
        vb = e.embed_query("近30天延迟发货订单有多少")
        assert not np.array_equal(va, vb)

    def test_batch_embed_shape(self):
        e = Embedder(mode="mock")
        out = e.embed_texts(["a", "b", "c"])
        assert out.shape == (3, 512)
        assert out.dtype == np.float32


class TestRealDegrade:
    def test_load_failure_degrades_to_mock_degraded(self, monkeypatch):
        # config.EMBEDDER_MODE 是 import 时读入的模块常量（conftest 已设 mock）——
        # 测试显式传 mode="real" 才能覆盖 real 加载失败分支
        def _boom(self):  # 模拟：模型文件缺失 / 卷未挂 / 断网
            raise RuntimeError("model file missing (hf offline)")

        monkeypatch.setattr(Embedder, "_load", _boom)
        e = Embedder(mode="real")
        assert e.mode == "mock"
        assert e.status_name() == "mock_degraded"
        assert e._load_error and "model file missing" in e._load_error
        # 降级后接口仍可用（服务不崩）
        assert e.embed_query("查询").shape == (512,)
        assert e.embed_texts(["x"]).shape == (1, 512)


class TestModelStatusRegistry:
    def test_record_and_snapshot(self):
        record_embedder("mock_degraded", "boom")
        record_reranker("bge-failed→rule", "oom")
        snap = snapshot()
        assert snap["embedder"] == "mock_degraded"
        assert snap["reranker"] == "bge-failed→rule"

    def test_probe_if_pending_probes_once_then_caches(self, monkeypatch):
        record_embedder("pending")
        record_reranker("pending")
        calls = {"embedder": 0, "reranker": 0}

        class _FakeEmbedder:
            def __init__(self):
                calls["embedder"] += 1
                record_embedder("mock")

        class _FakeReranker:
            def status(self):
                calls["reranker"] += 1
                record_reranker("rule")
                return "rule"

        # probe_if_pending 在函数体内 `from app.shared.rag... import`——patch 源头模块
        monkeypatch.setattr("app.shared.rag.embedder.Embedder", _FakeEmbedder)
        monkeypatch.setattr("app.shared.rag.reranker.get_reranker", lambda: _FakeReranker())
        probe_if_pending()
        assert calls["embedder"] == 1 and calls["reranker"] == 1
        # 已探测（非 pending）→ 二次探活不再触发加载（幂等）
        probe_if_pending()
        assert calls["embedder"] == 1 and calls["reranker"] == 1
