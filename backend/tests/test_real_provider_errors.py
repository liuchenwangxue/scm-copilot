"""W27 Day5 覆盖率冲刺 I：real 提供方错误分类（errors.py 纯函数）+ 降级链边界。

覆盖手册 Day5：
- `_retryable()` 边界矩阵：429/502/503/504/timeout 关键词/连接错误 → True；
  400/401/403 → False
- `_is_quota_error` / `_has_quota_kw`：额度耗尽关键词命中 → 触发模型切换
- `_degrade_or_raise()`：默认降 mock（generate_json 返回 dict + degraded=True、
  generate 返回 [WARNING] str）；额度耗尽错误不降级直接上抛；降级关 → 上抛
"""
import httpx
import pytest

from app.shared.llm.real import errors as err
from app.shared.llm.real_provider import RealLLMProvider


class TestRetryable:
    """`_retryable()` 边界矩阵。"""

    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 507])
    def test_provider_error_retryable_statuses(self, status):
        assert err._retryable(err._ProviderError(status, "boom")) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
    def test_provider_error_non_retryable_statuses(self, status):
        assert err._retryable(err._ProviderError(status, "boom")) is False

    def test_provider_error_without_status_retryable(self):
        assert err._retryable(err._ProviderError(None, "boom")) is True

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.TimeoutException("timed out"),
            httpx.ConnectError("conn refused"),
            TimeoutError(),
        ],
    )
    def test_network_errors_retryable(self, exc):
        assert err._retryable(exc) is True

    @pytest.mark.parametrize(
        "text",
        [
            "request timeout after 30s",
            "连接被拒绝",
            "503 Service Unavailable",
            "upstream 502",
            "gateway 504",
        ],
    )
    def test_keyword_text_retryable(self, text):
        assert err._retryable(RuntimeError(text)) is True

    def test_plain_error_not_retryable(self):
        assert err._retryable(RuntimeError("invalid argument")) is False
        assert err._retryable(ValueError("bad value")) is False


class TestQuotaKeywords:
    """额度耗尽关键词（命中 → 切模型而非普通重试）。"""

    @pytest.mark.parametrize(
        "text",
        [
            "quota exceeded",
            "insufficient balance",
            "rate limit",
            "access denied",
            "余额不足",
            "限流",
            "额度不足",
            "欠费",
            "maximum context",
        ],
    )
    def test_has_quota_kw_hits(self, text):
        assert err._has_quota_kw(text) is True, text

    def test_has_quota_kw_misses(self):
        assert err._has_quota_kw("bad request param") is False
        assert err._has_quota_kw("order not found") is False

    def test_is_quota_error_429(self):
        assert err._is_quota_error(err._ProviderError(429, "too many")) is True

    def test_is_quota_error_400_403_with_keyword(self):
        assert err._is_quota_error(err._ProviderError(400, "insufficient balance")) is True
        assert err._is_quota_error(err._ProviderError(403, "access denied")) is True

    def test_is_quota_error_client_error_without_keyword(self):
        assert err._is_quota_error(err._ProviderError(400, "bad schema")) is False
        assert err._is_quota_error(err._ProviderError(403, "forbidden")) is False

    def test_is_quota_error_text_fallback(self):
        assert err._is_quota_error(RuntimeError("model quota exhausted")) is True
        assert err._is_quota_error(RuntimeError("network glitch")) is False


class TestDegradeOrRaise:
    """`_degrade_or_raise()`：降级链边界（绕过 __init__，不依赖 Key/网络）。"""

    def _provider(self, degrade_to_mock: bool = True) -> RealLLMProvider:
        from app.shared.llm.mock_provider import MockLLMProvider

        p = RealLLMProvider.__new__(RealLLMProvider)
        p.degrade_to_mock = degrade_to_mock
        p._mock = MockLLMProvider()
        return p

    async def test_quota_error_raised_not_degraded(self):
        p = self._provider()
        with pytest.raises(err._ProviderError) as ei:
            await p._degrade_or_raise(err._ProviderError(429, "quota"), "generate", [], {})
        assert ei.value.status == 429

    async def test_generate_json_degrades_to_mock_dict(self):
        p = self._provider()
        js = await p._degrade_or_raise(RuntimeError("conn refused"), "generate_json", [], {})
        assert isinstance(js, dict), f"降级应返回 dict，实际 {type(js).__name__}"
        assert js.get("degraded") is True
        assert js.get("degrade_reason")

    async def test_generate_degrades_to_warning_str(self):
        p = self._provider()
        out = await p._degrade_or_raise(RuntimeError("conn refused"), "generate", [], {})
        assert isinstance(out, str)
        assert out.startswith("[WARNING]")

    async def test_degrade_disabled_raises(self):
        p = self._provider(degrade_to_mock=False)
        with pytest.raises(RuntimeError):
            await p._degrade_or_raise(RuntimeError("boom"), "generate", [], {})
