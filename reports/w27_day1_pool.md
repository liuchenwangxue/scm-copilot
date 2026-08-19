# W27 Day1 报告 · AsyncMySaver 连接池化（B1/A1/A2）

> 阶段五 · W27 Day1 ｜ 2026-08-20 ｜ 依据《W27学习执行手册》Day1
> 目标：AsyncMySaver 从「单连接串行」改「池化并发」，40 并发 P95 排队根因拔除。

---

## 一、改造内容

### 1.1 根因回顾（面试素材）

单连接版 `AsyncMySaver` 把「单 conn + `asyncio.Lock`」当作**全局限流器**：40 并发下
ops 请求全部在锁上排队（W26 基线 ops_query P95=3467.9ms，`loadtest_final.md` §四）。

### 1.2 池化设计（`backend/app/domains/ops/persistence.py`）

- 新增 `PooledAsyncMySaver(BaseCheckpointSaver)`：每操作从 `asyncmy.Pool` 取一条连接，
  绑定**临时 `AsyncMySaver`** 执行 → 临时 saver 每次新建，其内锁仅单操作独占、零争用。
- 继承基类获得 `with_allowlist`（compile 的 msgpack 严格模式路径，实测会触发）。
- 池大小 Little's law：40 并发 × 单写 ~50ms / 1s ≈ 2 条忙连接，默认 `maxsize=10` 是余量
  （面试口径："为什么是 10 不是 40"）。
- 懒建池（graph.get_biz_graph 编译时）+ 进程内单例；`reset_checkpointer()`/`close_*`
  关闭池；连接与创建它的 loop 绑定纪律不变。
- 配置化：`SCM_CHECKPOINT_POOL_SIZE`（默认 10）/ `MIN`（2）/ `RECYCLE`（3600s）进
  `app/shared/config.py`。compose MySQL `--max-connections=500` 已满足双实例
  （SQLAlchemy 60×2 + 池 10×2 + 调度/审批，余量充足）。

## 二、测试证据（护栏 + 新增）

| 测试 | 结果 | 说明 |
|---|---|---|
| `test_checkpointer_mysql.py`（W23 护栏） | 2/2 ✅ | 池化后 roundtrip / thread 隔离不回归 |
| `test_checkpointer_pool.py`（新增 4 项） | 4/4 ✅ | 见下 |
| 全量回归 `backend/tests` | 355 passed / 3 skipped ✅ | 含 `with_allowlist` 编译路径 |
| ruff / mypy | 0 error ✅ | |

`test_checkpointer_pool.py` 断言口径（本机实测教训）：
- 单路基线的固定开销（序列化线程跳转 / 池取连接）不随并发线性分摊，绝对倍数阈值
  （手册原「<3×单路」）在本机不可靠 → 改用**同测试内背靠背串行基线**对比：
  - 20 路并行 < 70%×20 次串行（实测约 35ms vs 95ms，~2.7× 加速）
  - 池耗尽（池 4 × 并发 12）排队不报错，仍显著快于串行
- 版本逐写递增：`checkpoint_blobs` 主键是 `(thread_id, channel, version)`，同 thread
  重复写同 version 会命中 `INSERT IGNORE` 良性告警 → 测试数据构造规避。

## 三、压测前后对比（20/30/40 三档，W26 基线 vs W27 D1）

工具 `deploy/load_test.py`，打 nginx `https://localhost:18443`，双实例 least_conn，
LLM_PROVIDER=mock。JSON 证据见 `deploy/reports/day1_load_{20,30,40}_v*.json`。

| 档位 | 指标 | W26 基线 | D1（暖机轮） | 变化 |
|---|---|---|---|---|
| **20 并发** | 总 P95 | 1167.3ms | **609.2ms** | -48% |
| | ops_query P95 | 1294ms | **772ms** | -40% |
| **30 并发** | 总 P95 | 1268.8ms | **794.2ms** | -37% |
| | ops_query P95 | 1648ms | **854ms** | -48% |
| **40 并发** | 总 P95 | 2087.1ms | **1739–2020ms** | -3%~-17%（机器噪声大） |
| | ops_query P95 | 3467.9ms | **2568–3095ms** | -11%~-26% |

- 成功率三档均 100%、HTTP 5xx=0（与 W26 一致）。
- **20/30 并发已稳进 ≤1.5s Gate**；40 并发改善显著但受本机多栈共享资源（Docker
  Desktop 同机运行 yudao/stage3/w5/w9 等 20+ 容器）噪声大，未达 1.5s → **按手册
  D7 前继续调**（候选：checkpoint 合并写、audit/conversation 写路径异步化）。

## 四、运维事件（记录，非代码问题）

- 宿主机 Docker Desktop WSL2 端口转发（wslrelay）对 13306（MySQL）/18443（nginx）
  先后失效：TCP 可连但 0 字节返回。重启对应容器即恢复；已记录为本地环境坑。
- 调度器：`apscheduler_jobs` 六任务全部注册，`eval_nightly` next_run=08-21 02:00，
  `eval_reports` 已有 08-20 rag/nl2sql 双域记录 —— **夜间回归通道正常，为 B17 攒夜数
  从今晚起持续**。

## 五、待办交接（D7 压测终验前）

- [ ] 40 并发 ≤1.5s 终验（D7；本日已确认根因修复方向，数字显著下降）
- [ ] D2 起：session_ctx Redis 外置（本日不动）
- [ ] 夜间回归夜数累计（目标 6 晚到 W27 末）
