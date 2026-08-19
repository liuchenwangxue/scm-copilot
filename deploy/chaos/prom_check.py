r"""演练五辅助：查 Prometheus 双实例指标，验证 least_conn 摘除/流量集中证据。

用法：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/prom_check.py [--range 15m]
"""
import argparse
import sys

import httpx

PROM = "http://localhost:19090"


def query(q: str) -> dict:
    r = httpx.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=15)
    r.raise_for_status()
    return r.json()["data"]["result"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range", default="15m")
    ap.parse_args()

    print("=== 双实例指标（Prometheus）===")
    # 抓取目标状态
    targets = httpx.get(f"{PROM}/api/v1/targets", timeout=10).json()["data"]["activeTargets"]
    for t in targets:
        if t["labels"].get("job") == "scm-backend":
            inst = t["labels"].get("instance")
            print(f"[target] {inst}: {t['health']}")

    # 各实例请求计数（QPS 曲线证据：实例 label）
    for metric in ("scm_http_requests_total", "http_requests_total"):
        try:
            rows = query(f'{{__name__=~"{metric}.*", instance=~"backend-a[12].*"}}')
            if rows:
                print(f"\n[{metric}] 实例维度计数：")
                for row in rows:
                    inst = row["metric"].get("instance", "?")
                    print(f"  {inst}: {row['value'][1]}")
                break
        except Exception as e:
            print(f"  (查询 {metric} 失败: {type(e).__name__})")

    # 实例 up 状态（杀掉瞬间应短暂 down）
    ups = query('up{job="scm-backend"}')
    print("\n[up] 实例存活状态：")
    for row in ups:
        inst = row["metric"].get("instance", "?")
        print(f"  {inst}: up={row['value'][1]}")


if __name__ == "__main__":
    main()
