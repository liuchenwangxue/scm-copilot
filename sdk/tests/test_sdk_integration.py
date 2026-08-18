"""SDK 集成测试：干净环境真实调平台（对应手册 Day5 验收）。

前置：平台已起（SCM_SDK_BASE_URL）+ MySQL seed（admin 凭证）。
429 用例需要 Redis（令牌桶）；Redis 不可用时平台 fail-open 放行 → 该用例 skip
（部署环境验证，与后端 fail-open 设计一致——配额是软约束）。

覆盖：
- 十行脚本：chat_stream 流式 + nl2sql 表格 + approvals 列待审（一条测试完整走通）
- 429 + Retry-After：打满独立 Key 的令牌桶 → ScmQuotaError + retry_after
- API Key 吊销后立即 401（机器身份生命周期验证）
"""

import pytest

from scm_client import ScmAuthError, ScmCopilot, ScmQuotaError

pytestmark = pytest.mark.integration


def test_ten_line_sdk_flow(platform_url: str, api_key: str):
    """十行脚本验收：chat 流式打印 + nl2sql 拿表格 + approvals 列待审（三接口全通）。"""
    client = ScmCopilot(platform_url, api_key=api_key)

    # ① chat_stream：SSE 流必须正常结束（done 事件收尾）
    events = list(client.chat_stream("你好"))
    assert events, "chat_stream 应至少返回一个 SSE 事件"
    types = [e.type for e in events]
    assert "done" in types, f"流应以 done 收尾，收到：{types}"

    # ② nl2sql：返回表格 + SQL 透出（mock 生成链路 + 只读沙箱执行）
    result = client.nl2sql("华东区域有多少订单？", as_dataframe=True)
    assert result.table is True, f"应返回表格，rejected_reason={result.rejected_reason}"
    assert result.sql, "生成 SQL 必须透出（可审计）"
    assert result.columns and result.rows, "应有列与行"
    assert result.df is not None and len(result.df) > 0, "as_dataframe 应可构造 DataFrame"

    # ③ approvals：列待审（接口连通 + 契约结构）
    pending = client.approvals.list_pending()
    assert isinstance(pending, list)
    for item in pending:
        assert item.approval_id and item.session_id, "审批项应含 HITL 恢复上下文"

    client.close()


def test_rate_limit_429_with_retry_after(platform_url: str, create_key):
    """429 用例：独立 Key 打满令牌桶（容量 10）→ ScmQuotaError + Retry-After。"""
    key = create_key("sdk-429")
    client = ScmCopilot(platform_url, api_key=key)
    hit_429 = False
    # 容量 10 + 独立桶 → 最多 10 次内应 429；Redis 不可用（fail-open）则一路 200
    for _ in range(30):
        try:
            client._request("GET", "/api/v1/auth/me")
        except ScmQuotaError as e:
            hit_429 = True
            assert e.code == "QUOTA_429"
            assert e.retry_after is not None and e.retry_after >= 1, "429 必须带 Retry-After"
            break
    if not hit_429:
        pytest.skip("Redis 不可用 → 平台限速 fail-open（部署环境验证 429）")
    client.close()


def test_revoked_key_immediately_401(platform_url: str, admin_token: str, create_key):
    """吊销后 Key 立即失效（enabled=0 软删除语义）。"""
    key = create_key("sdk-revoke")
    client = ScmCopilot(platform_url, api_key=key)
    assert client._request("GET", "/api/v1/auth/me").status_code == 200

    # 吊销该 key（直接以 admin JWT 调 DELETE）
    admin = ScmCopilot(platform_url, token=admin_token)
    keys = admin._request("GET", "/api/v1/admin/apikeys").json()["api_keys"]
    key_row = next(k for k in keys if k["name"] == "sdk-revoke")
    revoke = admin._request("DELETE", f"/api/v1/admin/apikeys/{key_row['key_id']}")
    assert revoke.json()["revoked"] is True
    admin.close()

    # 吊销后：同一 key 请求受保护端点 → 401 ScmAuthError
    with pytest.raises(ScmAuthError):
        client._request("GET", "/api/v1/auth/me")
    client.close()
