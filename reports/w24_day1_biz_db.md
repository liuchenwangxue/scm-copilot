# W24 Day1 学习执行日志 · 业务库与只读沙箱（8/24 周一）

> 阶段四 W24 · 核心产物 #1：scm_biz 六表 + 万级固定 seed + nl2sql_ro 只读账号

## 一、今日目标与达成

| 目标 | 状态 | 证据 |
|---|---|---|
| `models_biz.py` 六表 ORM（ENUM 状态 + 复合索引） | ✅ | `app/domains/data/models_biz.py`：suppliers/products/orders/order_items/inventory/shipments |
| Alembic 第二库迁移链（独立链，隔离 platform 版本树） | ✅ | `backend/alembic_biz/` + `alembic_biz.ini`；`upgrade head` 从零可重放 |
| `deploy/initdb/01_create_ro_user.sql` 只读账号 | ✅ | 建 scm_biz + `nl2sql_ro`（仅 SELECT）；compose 挂载 |
| `scripts/seed_biz.py` 固定 seed 万级生成器 | ✅ | random.seed(42) + 固定基准日期 BASE_DATE；TRUNCATE 幂等 |
| 数据质量验证 | ✅ | `scripts/verify_biz_data.py` 8 项全 PASS（金额勾稽/延迟率/状态分布/低库存） |
| 只读验证：SELECT 正常 / UPDATE 被拒 | ✅ | ERROR 1142 `UPDATE command denied` |
| Makefile 目标 | ✅ | `migrate-biz` / `seed-biz` / `reseed-biz` / `check-biz` |
| 测试用例 | ✅ | `test_biz_seed.py` 7 用例全绿；完整回归 75 passed（原 68 + 新 7） |
| CI 接入 | ✅ | ci.yml 加 biz 迁移 + seed + 校验和 + 测试 env |

## 二、实测数字

- **六表行数**：suppliers 40 ｜ products 500 ｜ orders 10,000 ｜ order_items 34,934 ｜ shipments 6,951 ｜ inventory 500
- **固定 seed 校验和**（`make check-biz`，连跑 3 遍完全一致）：

| 表 | rows | md5（关键字段聚合） |
|---|---|---|
| suppliers | 40 | cedaeb27d5eb30cdf48298248efa44cf |
| products | 500 | 634a3d1fbe87ebcd1bf6fd33b4842a80 |
| orders | 10,000 | a9648dcc649ddd0d6f1c9e3950fb571f |
| order_items | 34,934 | aaa6be5831bcf2617ae9347e7580f9bb |
| shipments | 6,951 | 55e973d7ad92e1b41da9a94e2e1270d8 |
| inventory | 500 | 7a103628c30e3bb81b80e4685c706889 |

- **数据质量（verify_biz_data.py，8 项 ALL PASS）**：
  - 近 30 天订单 4,163（≥1000）；近 7 天 1,067（≥300）——"近 N 天"评测类问题有数据可查
  - 状态分布：draft 4.8% / paid 20.3% / shipped 39.8% / done 29.7% / cancelled 5.3%（与目标 ±3pp）
  - 金额勾稽 mismatch=0；发货↔状态一致非法记录=0；明细行数越界=0
  - 延迟发货率 7.8%（545/6,951）；低库存占比 19.0%（95/500）
  - 供应商区域分布：华东/华北/华南/西南各 10
- **只读沙箱**：`nl2sql_ro` SELECT COUNT(*) 正常；`UPDATE orders SET ...` → **ERROR 1142 (42000) UPDATE command denied**
- **迁移链**：`alembic_biz.ini` downgrade base → upgrade head 一轮重放通过
- **测试**：`test_biz_seed.py` 7 passed；完整 `pytest backend/tests` → **75 passed**；ruff 0 / mypy 0（112 source files）

## 三、关键决策与踩坑记录

### 决策 1：业务库用"独立 Alembic 环境"而非"共享 versions 目录"
- 手册写"`alembic -x db=biz` 双迁移链"。我评估后采用**独立 `alembic_biz.ini` + 独立 `alembic_biz/` 目录**：
  - 共享 `version_locations` 时两条链同处一个版本树，`upgrade head` 会尝试把两个库的表交叉应用到对方库（platform 链 12 表会建进 biz，反之亦然）
  - 独立环境彻底隔离：`-c alembic_biz.ini` 指定 DSN 与 metadata（BizBase），行为最可预期
  - 代价：双份 env.py 模板；收益：迁移语义清晰、CI 可分别验证

### 坑 1：Windows configparser GBK 读 ini 中文注释崩溃
- **现象**：`alembic -c alembic_biz.ini` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte 0x93`（pos 10）
- **根因**：`configparser.read(encoding="locale")` 在中文 Windows 用 GBK，ini 里的中文注释（UTF-8 字节）解析失败
- **解决**：`alembic_biz.ini` 注释全部改英文 ASCII（ini 惯例本就该纯 ASCII；platform 的 alembic.ini 无中文所以一直正常）

### 坑 2：`models_biz.py` 类继承写错 Base
- **现象**：autogenerate 报 `NameError: name 'Base' is not defined`
- **解决**：业务库基类是 `BizBase`，六表全量继承修正

### 坑 3：GROUP_CONCAT 默认 1MB 截断校验和
- **现象**：order_items 34,934 行聚合时 `Row 31 was cut by GROUP_CONCAT()` → 校验和是"截断后"的假值
- **解决**：`check()` 前 `SET SESSION group_concat_max_len = 100000000`（session 级，不影响业务查询）

### 坑 4：seed 首遍与后续校验和不同（已澄清）
- 首遍跑时脚本仍在编辑中（GROUP_CONCAT 修复前后），修复后连跑 3 遍 md5 完全一致 → 确定性成立
- 根因：不是 RNG 漂移，是**校验和计算被截断 + 运行时脚本版本不同**；固定 seed 本身（random.seed(42) + BASE_DATE）逐行确定

### 坑 5：`ruff --fix` 破坏可读性
- `SIM` 规则把 `int((await ...))` 展开成怪异的空行括号格式，语法合法但难看
- 解决：`ruff format` 统一格式化；教训：`--fix` 后必跑 `ruff format`

## 四、纵深防御叙事（面试题 20:00 段 0.5h）

**Q：为什么校验闸之外还要只读账号？**

1. **闸可能有未知绕过**：sqlglot AST 校验是"确定性"的，但模型/解析器升级、边界 case（新方言特性、编码混淆）可能存在未知绕过路径——只靠校验闸是"单点信任"。
2. **权限层兜底**：即使恶意 SQL 穿过四道闸，`nl2sql_ro` 只有 SELECT，`UPDATE/DELETE/INSERT/DDL` 全部被 MySQL 拒绝（ERROR 1142）。攻击者最多读到数据，改不了数据。
3. **最小权限原则**：账号粒度收敛到"只读业务库"，即使被拖库也无法横向移动写平台库/系统库。
4. **层与层独立审计**：闸层（SQL 原文）与库层（连接来源）双重记录，取证可回放。

**追问"你敢让 LLM 生成的 SQL 直接跑吗"的满分答案**：不敢，所以我做了两层——第一层 AST 白名单（确定性、无逃逸），第二层 DB 权限（即使第一层漏了，也写不进任何数据）。今天第一层（W24-D2）未做，先完成第二层，正是"纵深防御先兜底"的顺序。

## 五、欠账清单

- [x] 今日 Gate：六表行数达标 ✅ + 重放一致 ✅ + 只读被拒 ✅ + biz 链可重放 ✅
- [ ] 无新增欠账（W23 遗留"40 并发 P95 达标"仍在 W24 Day1 评估窗口，见 w23_report §9）

## 六、明日预告（W24 Day2 安全四道闸）

- `sql_validator.py`：sqlglot 四道闸（单语句/仅SELECT/禁危险函数/强制LIMIT）——《03》1.1 节权威实现
- `executor.py` 只读沙箱执行器（3s 超时/行数上限/截断）
- `test_sql_validator.py` 每闸 ≥5 单测 + 20 条攻击用例 20/20 拦截
- 只读账号本轮已就位 → 明日直接对接执行器
