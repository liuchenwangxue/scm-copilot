"""★ W25 Day4 OpenAPI 规范化自查测试（手册 Day4 下午第 4、5 步自动化）。

覆盖：
1. **端点覆盖 100%**：遍历最终 OpenAPI 契约（`app.openapi()["paths"]`）断言每条业务端点有
   `summary + description + tags`，且 200 响应有 content schema
   （响应模型 100% 注解；流式端点为 text/event-stream）
2. **三分组浏览**：Swagger UI 可浏览的前提是每个端点挂 tag；
   错误响应统一 `Err` schema（app 级 `responses` 声明注入每个端点）
3. **版本化**：业务端点全部在 `/api/v1` 前缀下（SDK 好写的前提）
4. **契约校验**：`/openapi.json` 通过 `openapi-spec-validator`（OpenAPI 3.1 语法正确）

> 说明：FastAPI 新版 `include_router` 在 `app.routes` 里是延迟展开的
> `_IncludedRouter` 占位，故覆盖检查以 OpenAPI 契约（最终文档）为准——
> "文档即接口规范"的验证口径。
"""

import pytest

from app.main import app

try:  # openapi-spec-validator 是 dev 依赖（pyproject）；缺装时跳过校验项
    from openapi_spec_validator import validate as validate_openapi
except ImportError:  # pragma: no cover
    validate_openapi = None  # type: ignore[assignment]

# SSE 流式端点（手册坑：response_model 写不了流式，显式声明 event-stream 契约）
_STREAMING_PATHS = {"/api/v1/kb/chat", "/api/v1/ops/chat"}


def _api_operations():
    """返回 [(method, path, operation)]——全部业务端点（最终 OpenAPI 契约）。"""
    spec = app.openapi()
    out = []
    for path, item in spec["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, op in item.items():
            if isinstance(op, dict):
                out.append((method.upper(), path, op))
    return out


# ==================== 端点覆盖 100% ====================


def test_all_api_operations_have_summary_description_tags():
    """每条业务端点必须有 summary + description + tags（Swagger 三分组可浏览）。"""
    missing = []
    for method, path, op in _api_operations():
        if not op.get("summary") or not op.get("description") or not op.get("tags"):
            missing.append(f"{method} {path}")
    assert not missing, f"缺 summary/description/tags 的端点: {missing}"


def test_all_api_operations_have_response_content():
    """每条业务端点的 200 响应必须有 content schema（响应模型 100% 注解）。"""
    missing = []
    for method, path, op in _api_operations():
        resp = op.get("responses", {}).get("200", {})
        if not resp.get("content"):
            missing.append(f"{method} {path}")
    assert not missing, f"200 响应缺 content schema 的端点: {missing}"


def test_streaming_paths_declare_event_stream():
    """SSE 端点的 200 media type 必须是 text/event-stream（事件协议进文档）。"""
    spec = app.openapi()
    for path in _STREAMING_PATHS:
        op = spec["paths"][path]["post"]
        media = op["responses"]["200"]["content"]
        assert "text/event-stream" in media, f"{path}: 应为 text/event-stream（SSE 流式）"
        assert media["text/event-stream"]["schema"]["type"] == "string"


# ==================== 版本化 ====================


def test_all_api_paths_under_v1():
    """业务端点全部在 /api/v1 前缀下（版本化；SDK 好写）。"""
    bad = [path for _, path, _ in _api_operations() if not path.startswith("/api/v1/")]
    assert not bad, f"不在 /api/v1 下的端点: {bad}"


# ==================== 统一错误契约 ====================


def test_err_schema_in_components():
    """统一错误码 Err 必须出现在 components.schemas（OpenAPI 引用前提）。"""
    schemas = app.openapi().get("components", {}).get("schemas", {})
    assert "Err" in schemas
    err = schemas["Err"]
    assert {"code", "message", "trace_id"} <= set(err.get("properties", {}))


def test_4xx_5xx_responses_use_err():
    """所有 4xx/5xx 响应模型必须引用 Err（app 级 responses 声明注入每个端点）。"""
    spec = app.openapi()
    bad = []
    for path, item in spec["paths"].items():
        if not path.startswith("/api/"):
            continue
        for method, op in item.items():
            if not isinstance(op, dict):
                continue
            for status, resp in op.get("responses", {}).items():
                if not status.startswith(("4", "5")):
                    continue
                for c in resp.get("content", {}).values():
                    ref = c.get("schema", {}).get("$ref", "")
                    if not ref.endswith("/Err"):
                        bad.append(f"{method.upper()} {path} {status} -> {ref}")
    assert not bad, f"4xx/5xx 响应未统一 Err 的端点: {bad}"


# ==================== OpenAPI 3.1 契约校验 ====================


def test_openapi_json_validates():
    """/openapi.json 通过 openapi-spec-validator（契约语法正确）。"""
    if validate_openapi is None:
        pytest.skip("openapi-spec-validator 未安装（dev extra 缺失）")
    validate_openapi(app.openapi())


def test_openapi_has_version_and_groups():
    """版本号 + 三分组 tag 可浏览（Swagger 验收清单基础）。"""
    spec = app.openapi()
    assert spec["openapi"].startswith("3.")
    tags = {t for _, _, op in _api_operations() for t in (op.get("tags") or [])}
    assert {"auth", "kb", "ops", "data", "admin"} <= tags
