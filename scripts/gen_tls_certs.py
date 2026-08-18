"""★ W25 Day6：mkcert 本地 TLS 证书生成脚本。

用法：
    python scripts/gen_tls_certs.py

前置：本机已安装 mkcert（`choco install mkcert` / `scoop install mkcert` / winget）。
流程：
1. `mkcert -install`：把本地根 CA 装进系统信任库（首次需要，之后幂等）
2. `mkcert localhost scm.local`：生成 CN 含两个 hostname 的证书（手册坑：
   证书 CN 要含你实际访问的 hostname——localhost + 自定义域名都生成）
3. 输出到 deploy/nginx/certs/（nginx compose 挂载的目录）：
   - localhost+2.pem          证书链
   - localhost+2-key.pem      私钥

验证：
- `make tls` 后 `make up` → https://localhost:18443/health 可访问
- 浏览器不报证书错误（mkcert 根 CA 已装进系统信任库）
- 生产环境换正式证书（Let's Encrypt / 公司 CA）——见 docs/deploy.md
"""

import shutil
import subprocess
import sys
from pathlib import Path

CERT_DIR = Path(__file__).resolve().parents[1] / "deploy" / "nginx" / "certs"


def _run(cmd: list[str]) -> None:
    print(f"  > {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stdout)
        print(r.stderr, file=sys.stderr)
        raise RuntimeError(f"命令失败（rc={r.returncode}）：{' '.join(cmd)}")
    if r.stdout.strip():
        print(r.stdout.strip())


def _find_mkcert() -> str | None:
    """定位 mkcert：PATH 优先；其次 winget 安装位置（Windows 常用）。"""
    exe = shutil.which("mkcert")
    if exe:
        return exe
    local = Path.home() / "AppData" / "Local" / "Microsoft" / "WinGet"
    for cand in sorted((local / "Packages").glob("FiloSottile.mkcert*/mkcert.exe")):
        if cand.exists():
            return str(cand)
    return None


def main() -> None:
    mkcert = _find_mkcert()
    if mkcert is None:
        print("[TLS] 未找到 mkcert，请先安装：winget install FiloSottile.mkcert / choco install mkcert / scoop install mkcert")
        sys.exit(1)
    print(f"[TLS] 使用 mkcert: {mkcert}")

    CERT_DIR.mkdir(parents=True, exist_ok=True)

    print("[TLS] 1/3 安装本地根 CA 到系统信任库（首次需要，之后幂等）")
    _run([mkcert, "-install"])

    print("[TLS] 2/3 生成证书：localhost + scm.local（CN 含实际访问 hostname）")
    _run([mkcert, "-key-file", str(CERT_DIR / "localhost+2-key.pem"),
          "-cert-file", str(CERT_DIR / "localhost+2.pem"),
          "localhost", "scm.local"])

    print(f"[TLS] 3/3 证书就绪：{CERT_DIR}")
    for f in sorted(CERT_DIR.iterdir()):
        print(f"  - {f.name}")


if __name__ == "__main__":
    main()
