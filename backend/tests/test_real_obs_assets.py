"""W27 Day5 覆盖率冲刺 I：real 资产模块（cost.py / model_pool.py / obs.py）。

覆盖手册 Day5（real 系列 ≥60%）：
- cost：_parse_usage 兼容推理模型、_estimate_cost_yuan、_log_cost 落 jsonl
- model_pool：活跃模型持久化读/写、环境变量覆盖、reorder 置首
- obs：LangFuse 未启用 fail-open、_get 导入失败自动关闭
"""
import json
import sys

import pytest

from app.shared import config
from app.shared.llm.real import cost as _cost
from app.shared.llm.real import model_pool as _pool
from app.shared.llm.real import obs as _obs_mod


class TestCostParse:
    def test_parse_usage_basic(self):
        payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        assert _cost._parse_usage(payload) == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "reasoning_tokens": 0,
        }

    def test_parse_usage_reasoning_tokens(self):
        payload = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "completion_tokens_details": {"reasoning_tokens": 12},
            }
        }
        assert _cost._parse_usage(payload)["reasoning_tokens"] == 12

    def test_parse_usage_missing_usage(self):
        assert _cost._parse_usage({})["prompt_tokens"] == 0

    def test_estimate_cost_yuan(self, monkeypatch):
        monkeypatch.setattr(config, "COST_PRICE_INPUT", 2.0)
        monkeypatch.setattr(config, "COST_PRICE_OUTPUT", 8.0)
        usage = {"prompt_tokens": 1_000_000, "completion_tokens": 500_000}
        assert _cost._estimate_cost_yuan(usage) == pytest.approx(6.0)  # 2 + 4

    def test_estimate_cost_yuan_price_error_returns_zero(self, monkeypatch):
        monkeypatch.setattr(config, "COST_PRICE_INPUT", "bad")  # float() 抛异常 → 0
        assert _cost._estimate_cost_yuan({"prompt_tokens": 1}) == 0.0

    def test_log_cost_writes_jsonl(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
        monkeypatch.setattr(_cost, "_inc_cost_metrics", lambda *a, **kw: None)
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "reasoning_tokens": 2,
                 "total_tokens": 15}
        _cost._log_cost(usage, "model-x", "generate")
        line = json.loads((tmp_path / "cost_usage.jsonl").read_text(encoding="utf-8").strip())
        assert line["model"] == "model-x" and line["tag"] == "generate"
        assert line["prompt_tokens"] == 10 and line["reasoning_tokens"] == 2

    def test_log_cost_metrics_failure_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)

        def _boom(*a, **kw):
            raise RuntimeError

        monkeypatch.setattr(_cost, "_inc_cost_metrics", _boom)
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        _cost._log_cost(usage, "m", "t")  # 指标旁路失败不影响 jsonl 记录
        assert (tmp_path / "cost_usage.jsonl").exists()

    def test_inc_cost_metrics_records_prometheus(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "REPORTS_DIR", tmp_path)
        from app.shared.obs import metrics as m

        m.clear()
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        _cost._log_cost(usage, "m-metrics", "generate")
        rendered = m.render()
        assert "scm_llm_tokens_total" in rendered, "Prometheus 成本看板应有 token 计数"
        assert "scm_llm_cost_yuan_total" in rendered
        m.clear()


class TestModelPool:
    def test_load_active_model_none_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(_pool, "_state_file", lambda: tmp_path / "nope.json")
        assert _pool._load_active_model() is None

    def test_load_active_model_ok(self, monkeypatch, tmp_path):
        f = tmp_path / "state.json"
        f.write_text(json.dumps({"model": "m1", "updated_at": 1.0}), encoding="utf-8")
        monkeypatch.setattr(_pool, "_state_file", lambda: f)
        assert _pool._load_active_model() == "m1"

    def test_load_active_model_dirty_file(self, monkeypatch, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("not-json{{", encoding="utf-8")
        monkeypatch.setattr(_pool, "_state_file", lambda: f)
        assert _pool._load_active_model() is None

    def test_save_active_model(self, monkeypatch, tmp_path):
        f = tmp_path / "state.json"
        monkeypatch.setattr(_pool, "_state_file", lambda: f)
        _pool._save_active_model("kimi-x")
        assert json.loads(f.read_text(encoding="utf-8"))["model"] == "kimi-x"

    def test_pool_models_env_override(self, monkeypatch):
        monkeypatch.setenv("LLM_MODEL_POOL", "a, b, c")
        assert _pool._pool_models() == ["a", "b", "c"]

    def test_pool_models_default(self, monkeypatch):
        monkeypatch.delenv("LLM_MODEL_POOL", raising=False)
        assert _pool._pool_models() == list(_pool.DEFAULT_MODEL_POOL)

    def test_reorder_active_first(self, monkeypatch):
        monkeypatch.setattr(_pool, "_load_active_model", lambda: "m3")
        pool, start = _pool.reorder_pool_by_active(["m1", "m2", "m3", "m4"])
        assert pool == ["m3", "m4", "m1", "m2"] and start == 2

    def test_reorder_active_not_in_pool(self, monkeypatch):
        monkeypatch.setattr(_pool, "_load_active_model", lambda: "zzz")
        pool, start = _pool.reorder_pool_by_active(["m1", "m2"])
        assert pool == ["m1", "m2"] and start == 0


class TestObs:
    def test_generation_noop_when_disabled(self):
        obs = _obs_mod._Observability()
        obs.generation("n", "m", [], "out", None)  # LangFuse 未启用 → 不抛错
        assert obs._lf is None

    def test_get_import_failure_disables(self, monkeypatch):
        obs = _obs_mod._Observability()
        monkeypatch.setattr(obs, "enabled", True)
        monkeypatch.setitem(sys.modules, "langfuse", None)
        assert obs._get() is None
        assert obs.enabled is False, "LangFuse 导入失败自动关闭（fail-open）"
