"""Grafana 面板截图（W26 Day2 演练证据归档）。

用 Grafana 渲染 API（/render/d-solo/...）截取指定面板 PNG 到 deploy/reports/。
需 Grafana 运行（http://localhost:13001，admin/admin123）。

用法：
    python deploy/chaos/grafana_snapshot.py \
        --name chaos_mysql_kill \
        --url "http://localhost:13001/d/scm-business/scm-business?from=now-30m&to=now"
"""
import argparse
import sys
import time
from pathlib import Path

import httpx

GRAFANA_BASE = "http://localhost:13001"
GRAFANA_USER = "admin"
GRAFANA_PASS = "admin123"
OUT_DIR = Path(__file__).resolve().parents[1] / "reports"  # deploy/../reports


def snapshot(url: str, out: Path, width: int = 1200, height: int = 600) -> bool:
    """渲染面板：/render/d-solo/<uid>/<slug>?panelId=...&from=...&to=...
    URL 必须包含 from/to 时间窗参数（否则 Grafana 渲染 API 报 bad request）。"""
    # 登录拿 cookie（Grafana 渲染需要认证）
    login_url = f"{GRAFANA_BASE}/login"
    with httpx.Client(timeout=60, verify=False) as client:
        # 获取 csrf/登录
        r = client.post(
            login_url,
            json={"user": GRAFANA_USER, "password": GRAFANA_PASS},
            headers={"Content-Type": "application/json"},
        )
        if r.status_code not in (200, 302):
            print(f"[grafana] login failed: {r.status_code} {r.text[:200]}")
            return False
        # 渲染（需带 session cookie）
        render_url = url.replace("d/", "render/d-solo/")
        if "from=" not in render_url:
            render_url += "&from=now-30m&to=now"
        rr = client.get(render_url, timeout=90)
        if rr.status_code != 200:
            print(f"[grafana] render failed: {rr.status_code} {rr.text[:200]}")
            return False
        out.write_bytes(rr.content)
        print(f"[grafana] saved {out} ({len(rr.content)} bytes)")
        return True


def main() -> None:
    ap = argparse.ArgumentParser(description="Grafana 面板截图归档")
    ap.add_argument("--name", required=True, help="输出文件名（无扩展名）")
    ap.add_argument("--url", required=True, help="面板 URL（含 from/to）")
    ap.add_argument("--width", type=int, default=1200)
    ap.add_argument("--height", type=int, default=600)
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"w26_day2_{args.name}.png"
    ok = snapshot(args.url, out, args.width, args.height)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
