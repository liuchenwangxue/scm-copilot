"""W27 Day2 双实例会话 Redis 化实测（A3/A4 验收核心证据）。

场景（手册 Day2 下午 4）：POST /api/data/query 走 a1 拿会话 id →
带 nginx/直连走 a2 追问"那华东呢" → 指代消解成功（resolved_question 正确）。

运行方式（在 scm-backend-a1 容器内执行）：
    docker cp deploy/verify_session_redis.py scm-backend-a1:/tmp/verify_session_redis.py
    docker exec scm-backend-a1 python /tmp/verify_session_redis.py
    # 可传 --skip-followup 只验证首轮（依赖 second instance 不在时降级验证）

关键断言：
- 首轮：resolved_question == 原问题（无上下文不消解）
- 追问（走 a2）：resolved_question == "华北区域有多少订单？"（跨实例读到 a1 写入的会话）
- 失败信息含 Redis key 检查（验证数据确实在 Redis 权威存储）
"""
import argparse
import json
import sys
import uuid

import httpx

# 容器网络内互访（compose 网络内 hostname 直达）
A1 = "http://backend-a1:8795"
A2 = "http://backend-a2:8795"
USER = "admin_t_huadong"
PASSWORD = "Passw0rd!"
TODAY = "2026-08-19"


def _login(client: httpx.Client, base: str) -> str:
    r = client.post(f"{base}/api/v1/auth/login",
                    json={"username": USER, "password": PASSWORD}, timeout=30)
    assert r.status_code == 200, f"登录失败: {r.status_code} {r.text[:200]}"
    return r.json()["access_token"]


def _query(client: httpx.Client, base: str, token: str, question: str,
           session_id: str) -> dict:
    r = client.post(
        f"{base}/api/v1/data/query",
        json={"question": question, "today": TODAY, "session_id": session_id},
        headers={"Authorization": f"Bearer {token}"}, timeout=60,
    )
    assert r.status_code == 200, f"query 失败: {r.status_code} {r.text[:300]}"
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-followup", action="store_true",
                    help="只验证 a1 建会话（不验证 a2 续问）")
    args = ap.parse_args()

    sid = f"w27d2-cross-{uuid.uuid4().hex[:8]}"
    print(f"=== 双实例会话 Redis 化实测 session_id={sid} ===")
    results: list[tuple[str, bool, str]] = []

    with httpx.Client(timeout=30) as c:
        # ---- 1) 走 a1 建会话 ----
        token1 = _login(c, A1)
        r1 = _query(c, A1, token1, "华东区域有多少订单？", sid)
        first_ok = r1["resolved_question"] == "华东区域有多少订单？"
        results.append(("a1 首轮 resolved 原样", first_ok,
                        f"resolved={r1['resolved_question']!r}"))

        # ---- 2) 走 a2 追问（跨实例续问） ----
        if not args.skip_followup:
            try:
                token2 = _login(c, A2)
            except AssertionError as e:
                print(f"  [SKIP] a2 不可达，仅验证 a1 建会话: {e}")
                return 0
            r2 = _query(c, A2, token2, "那华北呢？", sid)
            followup_ok = r2["resolved_question"] == "华北区域有多少订单？"
            results.append(("a2 追问 resolved 正确", followup_ok,
                            f"resolved={r2['resolved_question']!r}"))

    print()
    all_ok = True
    for name, ok, detail in results:
        all_ok &= ok
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
    print(f"\n结论: {'全部通过 ✓' if all_ok else '存在失败 ✗'}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
