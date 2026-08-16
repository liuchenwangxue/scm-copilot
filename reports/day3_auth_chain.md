# Day3 学习执行日志 · 认证链路（8/19 周三）

> 阶段四 W23 · 核心产物 #3：登录走 MySQL、权限走依赖注入、写操作全审计

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `POST /api/auth/login`（bcrypt 校验 + 双令牌 + 审计） | ✅ | `app/platform/auth.py` |
| `POST /api/auth/refresh`（rotation 换新） | ✅ | 旧 refresh 吊销，复用 401 |
| `POST /api/auth/logout`（吊销名单） | ✅ | `token_blacklist` 表落库版 |
| `platform/rbac.py` `require_permission("...")` 依赖注入 | ✅ | 读 JWT claims 权限，零查库 |
| `platform/audit.py` ASGI 中间件（非 GET 落审计） | ✅ | 中间件 + 显式 write_audit |
| 白名单 `/health /docs /metrics` + 全局 JWT 门禁 | ✅ | `main.py` `global_auth` |
| 三态用例（401/403/200） | ✅ | `test_auth.py` 14 用例 |
| 权限矩阵（4 角色 × 12 权限码） | ✅ | `test_rbac.py` 17 用例 |
| `token_blacklist` 迁移可重放 | ✅ | downgrade→upgrade 通过 |

## 二、实测数字

- **测试**：`pytest backend/tests` → **37 passed**（auth 14 + rbac 17 + health 2 + seed 4）
- **三态覆盖**：
  - 401：无 token / 错凭证 / 未知用户 / 篡改签名 / 过期 / refresh 当 access / 吊销后访问 / 旧 refresh 重放
  - 403：viewer 访 admin 权限 / analyst 访 ops、admin 权限
  - 200：admin 登录 + `/api/auth/me` / 3 租户 × admin / 各角色合法路径
- **RBAC 矩阵**：4 角色 claims 权限与 seed 完全一致（admin=12 / operator=7 / analyst=4 / viewer=2）
- **越权正交**：11 组 allow/deny 参数化用例（有权限 200 / 无权限 403）全过
- **审计**：登录成功/失败 + 中间件非 GET 写操作都落 `audit_logs`；GET 不落
- **静态检查**：ruff 0 error + mypy 0 error（20 source files）
- **迁移**：`token_blacklist` downgrade→upgrade 重放通过

## 三、关键决策与踩坑记录

### 坑 1：FastAPI 依赖参数未标注类型 → 被当 query 参数（422）
- **现象**：`async def get_session(request)` 未标注 `Request` 类型，FastAPI 把 `request` 当成 **query 参数**，所有请求 422 `Field required`。
- **根因**：FastAPI 依赖注入靠**类型标注**识别特殊对象；无标注参数按 query 处理。
- **解决**：`request: Request` 显式标注。
- **启示**：所有 FastAPI 依赖注入参数必须写全类型，否则静默改变语义。

### 坑 2：`session.scalar(text("SELECT * ..."))` 返回 Raw Row 而非 ORM 对象
- **现象**：登录后 `user.password_hash` / `user.id` 报 AttributeError。
- **根因**：`SELECT *` 的 text 查询返回 `Row`，不是 `User` ORM 实例，无法访问列属性。
- **解决**：改用 `select(User).where(User.username == ...)` ORM 查询。

### 坑 3：write_audit 未 commit → 审计不落库
- **现象**：`test_login_audit_logged` 断言 `audit_logs` 有记录但为 0。
- **根因**：`get_session` 的 session 在请求结束关闭时 rollback 未提交的变更；`write_audit` 只 add 不 commit。
- **解决**：login/refresh/logout 三个端点显式 `await session.commit()`（失败登录也先提交再抛 401）。
- **启示**：审计这种"独立副作用"要显式提交，不能依赖请求生命周期隐式提交。

### 坑 4：篡改 token 测试改最后一位字符不可靠
- **现象**：改 JWT 末尾字符后仍 200（签名校验通过）。
- **根因**：base64url 尾部位可能因 padding 解码出相同字节，签名仍匹配。
- **解决**：改**签名段中间**字符，必然破坏 HMAC（`header.payload.signature` 三段式）。

### 决策 1：logout 吊销名单用落库版（`token_blacklist`）而非 Redis
- 手册允许"本周可先落库版"；compose 本期无 Redis，避免为单端点引入强依赖。
- 表按 `jti` 精确吊销 + 记录 `expires_at`（W25 迁 Redis 前可 `DELETE WHERE expires_at < NOW()` 清理）。

### 决策 2：认证端点自身落审计，中间件跳过（防双重落账）
- `login/refresh/logout` 各自 `write_audit`（login 记录 username、refresh/logout 记录 user_id）。
- 审计中间件 `SKIP_AUDIT_PATHS` 跳过这三个端点；其余非 GET 由中间件兜底全覆盖。

### 决策 3：JWT claims 缓存权限，RBAC 零查库
- 登录时把 `permissions` 塞进 access claims；`require_permission` 直接读 claims。
- 规避 Day2 面试题提到的"三级模型多一次 join"——权限判定不再每请求查库。

## 四、面试题（20:00 段 0.5h）

**Q：401 vs 403 语义边界 + JWT 双令牌为什么 access 短 refresh 长？**

答案要点：
1. **401 Unauthorized**：未认证/认证失败——没有有效身份（无 token / 过期 / 篡改 / 吊销）。客户端应重新登录。
2. **403 Forbidden**：已认证但权限不足——身份有效，但对该资源无权限。客户端不应重试，需换账号或申请权限。
3. **JWT 双令牌**：access 15min 短生命周期（泄露窗口小，被窃可快速作废）+ refresh 24h 长生命周期（用户少重新登录，体验好）。
4. **refresh rotation**：每次刷新换新 refresh 并吊销旧 refresh——即使 refresh 被窃，一次复用即失效，把泄露影响压缩到单次。

## 五、欠账清单

- [x] Day3 无欠账（Gate：登录→鉴权→越权 403 e2e 全过 ✅ + 写操作审计 100% ✅ + 三态+矩阵用例全绿 ✅）
- [x] `token_blacklist` 迁 Redis 记 W25 backlog（当前落库版够用）

## 六、明日预告（Day4 双域并入）

- 迁移 stage3-project-a/b 代码 → `app/domains/kb` `app/domains/ops`
- 共享层抽取 `app/shared/`（llm/rag/reliability 合并）
- 路由挂载 `/api/kb` `/api/ops` + 旧 109 项回归全绿
