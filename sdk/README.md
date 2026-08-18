# scm-copilot-client

SCM Copilot 供应链智能运营平台的官方 Python SDK——**十行代码接入**：流式问答、查数据、办审批。

薄封装 [httpx](https://www.python-httpx.org/)（零重依赖），同步方法 + SSE 迭代器两种风格。

## 安装

```bash
pip install scm-copilot-client            # 最小依赖（httpx）
pip install scm-copilot-client[dataframe] # 额外支持 nl2sql(as_dataframe=True)
```

## 快速上手（三接口）

先向平台管理员申请 API Key（`POST /api/v1/admin/apikeys`，明文只在创建时返回一次）：

```python
from scm_client import ScmCopilot

client = ScmCopilot("http://localhost:8000", api_key="sk-xxx")

# ① 流式问答（SSE 事件迭代器；event.delta 取打字机增量）
for event in client.chat_stream("供应商准入需要提交哪些资质材料？"):
    print(event.delta, end="", flush=True)

# ② 自然语言查数据（返回表格 + 透出 SQL，可审计可纠错）
result = client.nl2sql("近30天延迟发货 TOP5 供应商", as_dataframe=True)
print(result.sql)                       # 生成 SQL 100% 透出
print(result.df.head())                 # pandas.DataFrame

# ③ 审批（列待审 → 决策；session_id 回传恢复 HITL 会话）
pending = client.approvals.list_pending()
if pending:
    item = pending[0]
    client.approvals.decide(item.approval_id, "approve", reason="平台放行",
                            session_id=item.session_id)

client.close()
```

## 认证方式

| 方式 | 传参 | 场景 |
|---|---|---|
| API Key（推荐） | `ScmCopilot(url, api_key="sk-...")` | 机器身份 / 集成方 / CI——继承 owner 用户权限 |
| JWT | `ScmCopilot(url, token="<access_token>")` | 用户身份 / 交互式脚本 |

> 也可直接复用你自己的 `httpx.Client`（传 `client=` 参数），SDK 不接管连接管理。

## 事件协议（chat_stream）

SSE `data:` 行 JSON，`type` 字段分发（kb / ops 双域）：

| type | 含义 | 关键字段 |
|---|---|---|
| `progress` | 链路节点进展 | `node` / `data.result` |
| `message` | 打字机增量 | `content` / `delta` |
| `citations` | 引用溯源 | `citations` / `retrieved_docs` |
| `data_table` | 查数表格（kb 的 data 分支） | `columns` / `rows` / `sql` |
| `approval_request` | HITL 审批中断 | `approval_id` / `form` / `session_id` |
| `done` / `error` | 流结束 / 链路异常 | `error` |

## 错误处理

平台所有 4xx/5xx 统一 `{code, message, trace_id}`（OpenAPI 契约），SDK 映射为异常：

```python
from scm_client import ScmCopilot, ScmQuotaError, ScmAuthError

try:
    client.nl2sql("...")
except ScmAuthError as e:
    print("凭证无效或权限不足:", e.code)          # AUTH_401 / AUTH_403
except ScmQuotaError as e:
    print(f"限速超额，请 {e.retry_after}s 后重试:", e.code)  # QUOTA_429
except ScmError as e:
    print("其他错误:", e.code, e.trace_id)        # 用 trace_id 回平台排查
```

## 与平台契约对齐

- 三接口端点、请求/响应模型、错误码全部对齐后端 OpenAPI 3.1 契约（`/openapi.json`）；
- SDK 侧 `scm_client.errors.ErrorCode` 与后端 `errors.ErrorCode` 同一套常量；
- 端点增多时演进路径：`openapi-python-client` 代码生成（OpenAPI 已就绪）。

## 本地开发 / 测试

```bash
pip install -e ".[dev]"
pytest sdk/tests -v                  # 单元测试（MockTransport，离线可跑）
SCM_SDK_BASE_URL=http://localhost:8000 pytest sdk/tests/test_sdk_integration.py -v  # 真实平台
```
