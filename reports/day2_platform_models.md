# Day2 学习执行日志 · 平台库建模（8/18 周二）

> 阶段四 W23 · 核心产物 #2：五表模型 + Alembic 版本化 + seed 幂等

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| Alembic async 接 settings PLATFORM_DSN | ✅ | `backend/alembic/env.py` |
| 五表三级模型 ORM（SQLAlchemy 2.0 写法） | ✅ | `app/platform/models.py`：users/roles/permissions/role_permissions/user_roles |
| 伴随表（审计/审批/反馈/SDK/调度/会话） | ✅ | audit_logs/approvals/feedback/api_keys/quota_usage/scheduler_job_runs/conversations |
| autogenerate 迁移 + 人工 review | ✅ | `9e14aff7d28e_platform_core_tables.py`（12 表 + 索引 + DATETIME(3)） |
| 幂等种子脚本 | ✅ | `scripts/seed_platform.py`（连跑两遍一致） |
| 验证轮：downgrade→upgrade→seed×2 | ✅ | 全部通过，行数一致 |
| CI 补 migrate + seed 步骤 | ✅ | `ci.yml` 新增 migrate+seed（幂等两遍） |
| 种子数据测试用例 | ✅ | `test_seed_platform.py` 4 用例（integration） |

## 二、实测数字

- 迁移：`alembic downgrade base` → `upgrade head` 从零重放通过（12 表）
- seed 行数（连跑两遍完全一致，无重复告警）：
  - `roles`: 4 ｜ `permissions`: 12 ｜ `role_permissions`: 25 ｜ `user_roles`: 12 ｜ `users`: 12
- RBAC 矩阵：admin=12 / operator=7 / analyst=4 / viewer=2（合计 25 映射）
- 测试：`pytest backend/tests` → **6 passed**（/health ×2 + seed 验证 ×4）
- 静态检查：ruff 0 error + mypy 0 error（10 source files）

## 三、关键决策与踩坑记录

### 坑 1：`DATETIME(3)` 生成失败（1067 Invalid default value）
- **现象**：模型用 `sa.DateTime(3)` → autogenerate 产出 `DATETIME`（无精度）+ 默认值 `CURRENT_TIMESTAMP(3)`，MySQL 建表报 1067。
- **根因**：SQLAlchemy 2.0 里 `DateTime` 第一个位置参数是 `timezone`（非精度），`DateTime(3)` 的 3 被当 truthy timezone，不会生成 `DATETIME(3)`。
- **解决**：改用 MySQL 方言 `DATETIME(fsp=3)`（`from sqlalchemy.dialects.mysql import DATETIME`），封装 `_dt3()` 复用。
- **启示**：手册坑只提了"DATETIME(3) 默认值差 8 小时"，未提 `DateTime(3)` 位置参数陷阱，实测踩坑记入。

### 坑 2：`INSERT IGNORE` 幂等在 asyncmy 下刷重复告警
- **现象**：`INSERT IGNORE INTO role_permissions` 连跑两遍时爆 25 条 Duplicate entry 告警（虽 exit 0，但脏）。
- **解决**：改"先查后插"（`SELECT 1 WHERE ...` 不存在才 INSERT），与 users/permissions 一致，输出干净。

### 坑 3：`Index` 应从 `sqlalchemy` 而非 `sqlalchemy.orm` 导入
- **现象**：`from sqlalchemy.orm import Index` → ImportError。
- **解决**：`from sqlalchemy import Index`。

### 坑 4：seed 脚本 `ModuleNotFoundError: app`
- **现象**：从项目根跑 `scripts/seed_platform.py` 找不到 `app`。
- **解决**：脚本顶部 `sys.path.insert(0, .../backend)`（pytest 的 pythonpath 只对 pytest 生效）。

### 决策：alembic/versions 排除出 ruff
- autogenerate 生成的迁移脚本带旧风格（`Union`/`Sequence`/trailing whitespace），CI 会红。
- 在 pyproject `[tool.ruff] extend-exclude` 加 `backend/alembic/versions`（自动生成代码惯例不 lint）。

## 四、面试题（20:00 段 0.5h）

**Q：RBAC 三级模型（用户-角色-权限）对比 W21 配置式白名单的收益？**

答案要点：
1. **权限可运营**：配置式白名单写死在代码/配置文件，改权限要改码发版；三级模型下 `permissions`/`role_permissions` 落库，管理员界面可动态增删权限，无需发版。
2. **可审计**：权限本身的变更可落 `audit_logs`（谁在何时改了哪个角色），配置式白名单无法追溯权限变更。
3. **多租户可扩展**：`users.tenant_id` 是行级隔离键，三级模型天然支撑"每租户一套角色分配"，配置式白名单只能全局一套。
4. **复杂度代价**：多一次 join（user→user_roles→role_permissions），Day3 用 JWT claims 缓存权限码规避每请求查库。

## 五、欠账清单

- [ ] Day3 前无欠账（今日 Gate：upgrade 从零可重放 ✅ + seed 幂等 ✅ + CI 补步骤 ✅）

## 六、明日预告（Day3 认证链路）

- `platform/auth.py` 三端点（login/refresh/logout）+ 审计落库
- `platform/rbac.py` 依赖注入（`require_permission("ops:order:update")`）
- `platform/audit.py` ASGI 中间件
- `test_auth.py` 三态 + `test_rbac.py` 权限矩阵
