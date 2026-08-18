# W25 Day5 学习执行日志 · SDK 与配额（9/4 周五）

> 阶段四 W25 · 核心产物 #6：`pip install scm-copilot-client` 十行代码接入平台——开放生态落地
> 对应手册 Day5：SDK 三接口 / API Key 机器身份 / 令牌桶 429 / TestPyPI / SDK 集成测试 / CI SDK job

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| **SDK `sdk/scm_client/` 三接口**：chat_stream（SSE 迭代器）/ nl2sql（as_dataframe）/ approvals（list_pending + decide） | ✅ | `ScmCopilot` 主类 + `chat.py`（SSE 四型事件解析，data: 多行拼接 + 流尾兜底）/ `data.py`（as_dataframe 可选 pandas）/ `approvals.py` / `errors.py`（对齐 Err 契约）/ `models.py` |
| **统一 `ScmError`（带平台错误码）+ trace_id** | ✅ | `ScmAuthError`(401/403) / `ScmQuotaError`(429+Retry-After) / 基类 `ScmError`（code/message/trace_id 从 Err body 解析，非 JSON 兜底） |
| **`platform/apikeys.py`**：`sk-` 前缀 + secrets 生成 + sha256 哈希落库（明文只返回一次） | ✅ | `generate_api_key()`（sk- + 48 hex，192bit 熵）/ `hash_api_key`（sha256）/ 集成测试断言"创建返回明文、列表不暴露哈希" |
| **令牌桶限速**（Redis Lua 原子，容量 10 / 速率 5/min）→ 429 + Retry-After | ✅ | `check_token_bucket` Lua 原子脚本 + 纯逻辑 `token_bucket_allow` 可单测；实测 11 次请求后 `429 + Retry-After: 12`，body `QUOTA_429` |
| **认证双轨 `api_key_or_jwt`**（API Key 与 JWT 并存） | ✅ | `rbac.require_permission` 认证入口升级为 `api_key_or_jwt`；全局门禁 `global_auth` 识别 `sk-` 前缀（门禁只认证不限速，限速每请求恰好一次） |
| **API Key 管理 API**（admin 创建/列表/吊销 + seed 补权限） | ✅ | `POST/GET /api/v1/admin/apikeys` + `DELETE .../{id}`（enabled=0 软删除保审计）；seed 新增 `admin:apikey:manage`（admin 全量 13 权限） |
| **审批列表 API**（SDK list_pending 数据源，含 HITL 恢复上下文 session_id） | ✅ | `GET /api/v1/ops/approvals`（权限 ops:approval:manage；pending 优先 + diff + session_id 断点恢复） |
| **SDK 打包**（`python -m build`） | ✅ | `scm_copilot_client-0.1.0.tar.gz` + `py3-none-any.whl` 构建成功（sdk/dist/） |
| **`tests/test_sdk_integration.py`** 干净环境真实调平台 + 429 用例 | ✅ | 3 passed：十行脚本（chat 流式 + nl2sql 表格 + approvals 列待审）/ 429 + Retry-After / 吊销后立即 401 |
| **CI 加 SDK job**（装包 → 起平台 → 跑集成测试） | ✅ | `.github/workflows/ci.yml` 新增 `sdk` job：migrate+seed → 后台 uvicorn → SDK 单元 + 集成测试 |

## 二、实测数字

| 项 | 值 |
|---|---|
| SDK 单元测试（MockTransport 离线） | **10 passed**（SSE 解析 / 错误映射 / 请求构造 / 认证头） |
| SDK 集成测试（真实平台，MySQL+Redis） | **3 passed**（十行脚本 / 429+Retry-After / 吊销 401） |
| 后端 API Key + 审批列表测试 | **14 passed**（令牌桶纯逻辑 5 + 生命周期集成 9） |
| 全量回归 | **304 passed**（原 290 + 新增 14），0 failed |
| 静态检查 | ruff 0 error / mypy 0 error（181 source files，含 sdk） |
| 429 实测 | 独立桶第 11 次请求 → `429 + Retry-After: 12` + `QUOTA_429` + trace_id |
| 密钥泄露面 | 创建响应明文一次；列表仅 `key_prefix`（sk- + 前 8 位），不返回哈希 |
| 权限矩阵 | admin 13（新增 `admin:apikey:manage`）/ operator 7 / analyst 4 / viewer 2 |

### 2.1 十行脚本（SDK 集成测试全链路，真实数据）

```python
from scm_client import ScmCopilot
client = ScmCopilot("http://127.0.0.1:8001", api_key="sk-...")
for event in client.chat_stream("你好"):
    print(event.delta, end="", flush=True)      # SSE 流式 → done 收尾
result = client.nl2sql("华东区域有多少订单？", as_dataframe=True)
print(result.sql)                                # SQL 透出 + DataFrame 构造
pending = client.approvals.list_pending()        # 审批列表（含 session_id）
client.close()
```

### 2.2 API Key 生命周期（集成测试逐项验证）

| 步骤 | 结果 |
|---|---|
| admin 创建 Key（owner 缺省=当前用户） | 200，明文 `sk-` 一次返回 |
| API Key 访问 `/api/v1/auth/me` | 200，`username=admin_t_huadong` + 13 权限 |
| 列表不暴露哈希/明文 | `"key_hash"` / `"api_key":` 均不在响应体 |
| viewer 属主 Key 访问 admin 端点 | 403（权限继承正确收窄） |
| operator 访问管理 API | 403（`admin:apikey:manage` 权限闸） |
| 吊销后同一 Key | 401（enabled=0 软删除立即生效） |

## 三、关键决策与踩坑记录

### 决策 1：Key 哈希 sha256 而非 bcrypt（手册坑）
- **权衡**：bcrypt 100ms 校验不适合每请求高频路径；API Key 128bit 熵足够高，防猜测靠熵而非慢哈希；密码 bcrypt（低频 + 撞库防护）与 Key sha256（高频 + 熵防护）**策略分开**——面试可讲
- 落库仅哈希；明文创建时一次性返回（后续查询只展示前缀）

### 决策 2：限速只做一次（全局门禁只认证不限速）
- 若门禁与端点依赖各限一次 → 一次请求消耗 2 个令牌（计数虚高 + 429 提前）
- 方案：`global_auth` 对 `sk-` 只认证（校验 key/owner）；`api_key_or_jwt` 端点依赖内限速（每请求恰好一次）

### 决策 3：fail-open 语义分层
- 令牌桶 Redis 挂 → 放行（配额是软约束，不因 Redis 抖动拒绝集成方）——429 保护在 Redis 恢复后自动生效
- 429 用例设计成"打到 429 断言 Retry-After，打不到（Redis 挂）则 skip"——本地部署环境真验证，CI 无 Redis 自动跳过

### 决策 4：SDK 自定义 client 也补认证头
- 用户传入自定义 `httpx.Client`（MockTransport/连接池）时，SDK 不接管连接管理，但**必须补 Authorization 头**（实测遗漏导致集成测试 401 场景覆盖不到）

### 坑 1（★ 配置 bug）：Redis 默认端口 16380 指向他项目
- **现象**：本地 429 集成测试永远 fail-open（Redis 连不上）；compose 映射 16381（注释：本机 16380 被 stage3 占用）
- **修复**：`shared/config.py` + `platform/settings.py` 默认端口改 16381；修复后 429 用例实测 `Retry-After: 12`

### 坑 2：httpx.Request 无 `.json()` 方法（mypy 类型）
- MockTransport handler 里 `request.json()` 运行时存在但 mypy 类型缺失 → 改 `json.loads(request.content)`

### 坑 3：seed 权限断言连锁更新
- 新增 `admin:apikey:manage` 后，`test_seed_platform`（perms 12→13 / rp 25→26 / admin 12→13）与 `test_rbac`（EXPECTED_PERMISSIONS + len(admin)==13）4 个用例需同步——**权限单一事实来源在 seed_platform.py，测试逐字对齐**

## 四、验收（手册 Day5 验收项）

| 验收项 | 结果 |
|---|---|
| 干净环境 10 行跑通三接口 | ✅ SDK 集成测试 `test_ten_line_sdk_flow`（chat 流式 + nl2sql 表格 + approvals 列待审） |
| 429 + Retry-After 过 | ✅ `test_rate_limit_429_with_retry_after`（Retry-After: 12 + QUOTA_429） |
| SDK 测试绿 | ✅ 单元 10 + 集成 3 + 全量回归 304 |
| TestPyPI 可装 | 🚧 本地 `python -m build` 产物就绪（sdk/dist/）；twine 上传需 TestPyPI 账号（README 已给命令，备选名 `scm-copilot-client-dev`） |

## 五、面试题 0.5h：ADR-08——SDK 为什么手写不生成？

> **手写三接口的可控性**：
> 1. **SSE 迭代器体验**：`chat_stream` 要的是"事件迭代器 + delta 便捷属性"这种工程体验，代码生成器给的是函数签名不是流式体验；
> 2. **错误语义**：`ScmQuotaError(retry_after)` / `ScmAuthError` 的领域化异常映射，需要手写；生成代码只有通用 HTTP 错误；
> 3. **依赖纪律**：httpx 单依赖的"薄封装"承诺，生成器会拖入 requests/urllib3 全家桶。
>
> **演进路径（不是封闭决策）**：`/openapi.json` 契约已就绪（Day4），端点规模增长后切 `openapi-python-client` 生成——先手写把三接口做精，再生成把百端点做全。面试话术："**接口少时手写换可控性，接口多时生成换生产力，OpenAPI 契约让两条路都能走。**"

## 六、欠账 / 次日衔接（W25 Day6 优先）

- [ ] **TestPyPI 实传**：注册账号后 `twine upload --repository testpypi sdk/dist/*` + 干净 venv `pip install --index-url https://test.pypi.org/simple scm-copilot-client` 复验十行脚本（手册 Day5 下午任务 4；今天已完成本地 build + 集成测试，只差账号）
- [ ] **24h 零重复观测聚合**：Day3 启动的观测，Day6 早出 job_runs 证据表（周 Gate）
- [ ] eval_nightly 第二晚报告 → 7 日均值偏离正式生效
- [ ] Day6 三吸收项：Hooks + node-exporter/cAdvisor + mkcert TLS
- [ ] W23 遗留"40 并发 P95"评估（Day6 周 Gate 自检）

## 七、W25 周 Gate 进度

| Gate | 状态 |
|---|---|
| 双实例任务零重复（24h） | 🚧 观测进行中（Day3 启动，Day6 早聚合） |
| KB 同步 ≤5min | ✅ Day2 实测通过 |
| 日报准点 5/5 | 🚧 机制 + 首份实测通过 |
| SDK pip 十行跑通 | ✅ **Day5 集成测试真实跑通（chat/nl2sql/approvals 三接口）** |
| 429 用例过 | ✅ **Day5 实测 429 + Retry-After: 12** |
| OpenAPI 校验 + 覆盖 100% + 三分组 | ✅ Day4 通过（本次新增端点自带 summary/response_model，全量回归含 openapi 自查 8/8） |
