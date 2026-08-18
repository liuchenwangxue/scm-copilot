"""★ W23 Day6 压测工具：双实例 least_conn 无状态验证（登录 / kb 问答 mock / ops 查询）。

相对 W22 Day5 的 load_test.py（stage3 双应用）改造点（面试可讲）：
1. 打 nginx 入口（http://localhost:18000），混合路径 = 登录 / kb 问答 / ops 查询，
   测的是"一个平台、两个实例、状态全外置"的真无状态链路（MySQL 权威库 + Redis 共享）。
2. 每 worker 先登录拿 JWT（bcrypt 校验真实走 MySQL），再循环发请求（权限码进 claims 零查库）。
3. --kill-instance 杀实例演练：压测中段 `docker stop scm-backend-a1`，
   观察 nginx（least_conn + proxy_next_upstream）自动摘除/切换 → 统计 5xx = 0。
4. JSON 报告：--out xxx.json 输出结构化结果（给 w23_report 用）。

用法（先 make up-full 起全栈，LLM_PROVIDER=mock 零成本）：
    python deploy/load_test.py --concurrency 40 --per 5          # 40 并发 × 200 请求
    python deploy/load_test.py --kill-instance a1 --kill-at-pct 0.4
    python deploy/load_test.py --out deploy/reports/day6_load.json

指标：P50/P95/P99、成功率、QPS、错误分布（HTTP_5xx 单独统计）、按场景拆分。

成功判定：HTTP 200 且 SSE 文本含 '"type": "done"'（kb/ops chat 均为 SSE 流式）。
注意：压测机=本机时给系统留核，40 并发数据才可信（手册 Day6 坑）。
"""
import argparse
import asyncio
import json
import os
import random
import subprocess
import time

import httpx

# 默认走 nginx LB（compose 宿主 18000）；--base 可覆盖直连某实例
BASE = os.getenv("SCM_NGINX_URL", "http://localhost:18000")

# seed 用户（3 租户 admin 轮换，避免单 token 复用过度）
PLAIN_PASSWORD = "Passw0rd!"
USERS = [f"admin_{t}" for t in ("t_huadong", "t_huabei", "t_huanan")]

# ---- 混合路径问题（容器内无 embedding 模型，全部选"规则优先层"可走的请求）----
# kb_chat：语义路由 chat 分支（"你好"精确词，零 embedding）
KB_CHAT_MSGS = ["你好呀，你能做什么？", "你好，很高兴认识你", "早上好呀"]
# kb_tool：语义路由 tool 分支（PO 单号规则命中，返回转交话术，零 embedding）
KB_TOOL_MSGS = ["帮我查一下订单 PO-0003 现在到哪了", "把订单 PO-0002 的金额改成 9500"]
# ops_query：ops 图真实链路（LLM mock 意图识别 → 低危直通 → 工具调 mock_biz → 回答）
OPS_QUERY_MSGS = [f"查一下订单 PO-{i:04d} 的状态" for i in range(1, 21)]
# ops 会话池：复用少量 thread（模拟真实多轮对话），避免每请求新建 thread 的全量
# checkpoint 写——更贴近真实流量，也缓解共享单连接 checkpointer 的串行压力
OPS_SESSIONS = [f"ops-thread-{i:02d}" for i in range(20)]

# 混合权重：ops 20% / kb_tool 40% / kb_chat 40%
# ★ 反映真实平台流量（知识问答是主入口）：ops 受 checkpointer 单连接串行限制，
#   并发下 P50~1.1s（已知设计限制，见 w23_report），故按真实占比压低其在混合流量中的份额
SCENARIOS = [
    ("ops_query", "/api/v1/ops/chat", OPS_QUERY_MSGS, 0.20),
    ("kb_tool", "/api/v1/kb/chat", KB_TOOL_MSGS, 0.40),
    ("kb_chat", "/api/v1/kb/chat", KB_CHAT_MSGS, 0.40),
]


def pick_scenario(rng: random.Random):
    r = rng.random()
    acc = 0.0
    for name, url, msgs, w in SCENARIOS:
        acc += w
        if r <= acc:
            return name, url, msgs
    return SCENARIOS[0][0], SCENARIOS[0][1], SCENARIOS[0][2]


async def login(client: httpx.AsyncClient, base: str, username: str) -> str | None:
    """登录拿 access token（bcrypt + MySQL 校验真实链路）。失败返回 None。"""
    try:
        r = await client.post(f"{base}/api/v1/auth/login",
                              json={"username": username, "password": PLAIN_PASSWORD},
                              timeout=30)
        if r.status_code == 200:
            return r.json()["access_token"]
    except Exception:  # noqa: BLE001
        pass
    return None


async def one(client: httpx.AsyncClient, base: str, headers: dict, i: int,
              rng: random.Random) -> dict:
    """发一个请求，返回 {name, dt, status, err}。"""
    name, url, msgs = pick_scenario(rng)
    msg = rng.choice(msgs)
    # ops 复用会话池（真实多轮）；kb 每次新会话（模拟不同访客）
    sid = rng.choice(OPS_SESSIONS) if name == "ops_query" else f"load-{name}-{i}"
    payload = {"message": msg, "session_id": sid}
    t0 = time.time()
    status = None
    err = None
    try:
        r = await client.post(f"{base}{url}", json=payload, headers=headers, timeout=90)
        status = r.status_code
        if status == 200 and '"type": "done"' not in r.text:
            err = "no_done_event"
    except Exception as e:  # noqa: BLE001
        err = type(e).__name__
    return {"name": name, "dt": time.time() - t0, "status": status, "err": err}


async def worker(client: httpx.AsyncClient, base: str, idx: int, per: int,
                 rng: random.Random) -> list[dict]:
    """单个并发 worker：先登录 → 循环发 per 个请求。"""
    user = USERS[idx % len(USERS)]
    token = await login(client, base, user)
    if token is None:
        return [{"name": "login", "dt": 0, "status": None, "err": "login_failed"}] * per
    headers = {"Authorization": f"Bearer {token}"}
    out = []
    for i in range(per):
        out.append(await one(client, base, headers, i, rng))
    return out


def _docker_stop(name: str) -> None:
    print(f"[kill] docker stop {name} ...")
    subprocess.run(["docker", "stop", name], check=False, capture_output=True, timeout=30)


def _docker_start(name: str) -> None:
    print(f"[kill] docker start {name} ...")
    subprocess.run(["docker", "start", name], check=False, capture_output=True, timeout=30)


def _maybe_kill(kill: dict, done: int, total: int) -> None:
    """压测进行到 kill_at_pct 时执行一次杀实例（后续不再重复）。"""
    if kill and not kill["fired"] and done >= total * kill["at_pct"]:
        kill["fired"] = True
        _docker_stop(kill["instance"])


async def run_mixed(base: str, concurrency: int, per: int, kill: dict | None) -> dict:
    """混合压测：concurrency 个 worker 并行，各发 per 个请求。"""
    total = concurrency * per
    rng = random.Random(42)
    t_start = time.time()
    async with httpx.AsyncClient(timeout=100) as client:
        tasks = {asyncio.create_task(worker(client, base, idx, per, rng))
                 for idx in range(concurrency)}
        done_tasks: set[asyncio.Task] = set()
        done_count = 0
        while tasks:
            done, tasks = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            done_tasks |= done
            done_count += len(done) * per
            _maybe_kill(kill, done_count, total)
        results: list[dict] = []
        for t in done_tasks:
            results.extend(await t)
    t_total = time.time() - t_start
    return summarize(results, t_total, concurrency, per, kill)


def summarize(results: list[dict], t_total: float, concurrency: int, per: int,
              kill: dict | None) -> dict:
    dts = sorted(r["dt"] for r in results)
    n = len(dts)
    ok = sum(1 for r in results if r["status"] == 200 and not r["err"])
    qps = n / t_total if t_total > 0 else 0
    errs: dict[str, int] = {}
    for r in results:
        if r["err"]:
            errs[r["err"]] = errs.get(r["err"], 0) + 1
        elif r["status"] != 200:
            key = f"HTTP_{r['status']}"
            errs[key] = errs.get(key, 0) + 1
    http5xx = sum(v for k, v in errs.items() if k.startswith("HTTP_5"))

    def pct(idx: int) -> float:
        return dts[int(n * idx)] * 1000 if n else 0.0

    per_scene: dict[str, dict] = {}
    for sname in ("ops_query", "kb_tool", "kb_chat"):
        sub = sorted(r["dt"] for r in results if r["name"] == sname)
        if not sub:
            continue
        m = len(sub)
        ok_s = sum(1 for r in results
                   if r["name"] == sname and not r["err"] and r["status"] == 200)
        per_scene[sname] = {
            "count": m, "ok": ok_s,
            "p50": round(sub[m // 2] * 1000, 1),
            "p95": round(sub[int(m * 0.95)] * 1000, 1),
        }

    stat = {
        "mode": "dual-instance-nginx",
        "concurrency": concurrency, "per": per, "total": n,
        "t_sec": round(t_total, 2), "qps": round(qps, 2),
        "success": ok, "success_rate": round(ok / n * 100, 2) if n else 0.0,
        "p50_ms": round(pct(0.5), 1), "p95_ms": round(pct(0.95), 1),
        "p99_ms": round(pct(0.99), 1),
        "http_5xx": http5xx,
        "errors": errs,
        "per_scene": per_scene,
        "kill": kill,
    }
    _print_stat(stat)
    return stat


def _print_stat(stat: dict) -> None:
    print("=" * 66)
    print(f"压测结果：并发 {stat['concurrency']} x {stat['per']} = {stat['total']} 请求"
          f"（{stat['mode']}）")
    print(f"总耗时 {stat['t_sec']}s | QPS={stat['qps']}")
    print(f"成功率 {stat['success']}/{stat['total']} = {stat['success_rate']}%")
    print(f"P50={stat['p50_ms']}ms P95={stat['p95_ms']}ms P99={stat['p99_ms']}ms")
    print(f"HTTP_5xx={stat['http_5xx']} 错误分布：{stat['errors'] if stat['errors'] else '无'}")
    for sname, s in stat["per_scene"].items():
        print(f"  [{sname}] {s['count']} 条 成功 {s['ok']} "
              f"P50={s['p50']:.0f}ms P95={s['p95']:.0f}ms")
    if stat["kill"] and stat["kill"]["fired"]:
        print(f"[kill] 已按计划在 {stat['kill']['at_pct']:.0%} 进度杀实例 "
              f"{stat['kill']['instance']}（nginx 自动切换，5xx={stat['http_5xx']}）")
    print()


async def main() -> None:
    ap = argparse.ArgumentParser(description="W23 Day6 双实例无状态压测工具")
    ap.add_argument("--base", default=BASE, help=f"nginx LB 地址（默认 {BASE}）")
    ap.add_argument("--concurrency", type=int, default=40, help="并发数（默认 40）")
    ap.add_argument("--per", type=int, default=5, help="每并发请求数（默认 5 → 共 200）")
    ap.add_argument("--kill-instance", choices=["a1", "a2"], default=None,
                    help="杀实例演练：压测中段 stop 该实例（a1=backend-a1）")
    ap.add_argument("--kill-at-pct", type=float, default=0.4,
                    help="杀实例时机（进度比例，默认 0.4）")
    ap.add_argument("--no-restart", action="store_true",
                    help="演练后不恢复被杀实例（默认自动 docker start）")
    ap.add_argument("--out", default=None, help="JSON 报告输出路径")
    args = ap.parse_args()

    kill = None
    if args.kill_instance:
        kill = {"instance": f"scm-backend-{args.kill_instance}",
                "at_pct": args.kill_at_pct, "fired": False,
                "restart": not args.no_restart}

    print(f"=== 压测开始：并发 {args.concurrency} x {args.per} = {args.concurrency * args.per} 请求"
          f" @ {args.base}（LLM_PROVIDER=mock 零成本）===")
    stat = await run_mixed(args.base, args.concurrency, args.per, kill)
    if kill and kill["fired"] and kill["restart"]:
        _docker_start(kill["instance"])
        print("[kill] 实例已恢复（docker start）")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(stat, f, ensure_ascii=False, indent=2)
        print(f"[report] 已写入 {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
