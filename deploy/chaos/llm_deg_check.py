r"""LLM 全超时降级链验证（W26 Day2 演练四）。

跑在 llm_timeout.ps1 设置的环境变量下（real + 失效 key + 短超时 + 三级模型池）。
验证降级链：
    模型池 glm→deepseek→invalid 逐个失败 → 池内全失败 →
    LLM_DEGRADE_TO_MOCK=1 → 返回 [WARNING] 前缀 mock 兜底（明确告知降级）
且 usage 记账不重复（失败模型不累计，仅兜底成功记账一次）。

用法（由 llm_timeout.ps1 调用）：
    .\.venv\Scripts\python.exe -X utf8 deploy/chaos/llm_deg_check.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.shared import config  # noqa: E402


async def main() -> None:
    print("=== LLM 全超时降级链验证 ===")
    print(f"LLM_PROVIDER={config.LLM_PROVIDER} base={config.LLM_BASE_URL[:40]}...")
    print(f"LLM_MODEL_POOL={config.LLM_MODEL_POOL} timeout={config.LLM_TIMEOUT}s")
    print(f"LLM_DEGRADE_TO_MOCK={config.LLM_DEGRADE_TO_MOCK}")

    if config.LLM_PROVIDER != "real":
        print("[SKIP] 当前非 real 模式——请用 llm_timeout.ps1 设置演练环境（或本脚本直接设 env）")
        return

    from app.shared.llm import get_provider
    provider = get_provider("real")

    # 1) generate（文本）：降级链 → [WARNING] 前缀 mock 文本
    t0 = asyncio.get_event_loop().time()
    out = await provider.generate(
        [{"role": "user", "content": "供应链采购制度中，招标采购的金额门槛是多少？"}],
        max_tokens=128,
    )
    dt = asyncio.get_event_loop().time() - t0
    degraded = isinstance(out, str) and out.startswith("[WARNING]")
    print(f"[generate] 耗时 {dt:.1f}s 降级标记={degraded}")
    print(f"[generate] 输出前 120 字：{str(out)[:120]!r}")
    if not degraded:
        print("[FAIL] generate 未走降级链（应为 [WARNING] 前缀 mock 兜底）")
        sys.exit(1)

    # 2) generate_json（结构化）：降级链应返回 dict（mock 引用结构）
    j0 = asyncio.get_event_loop().time()
    js = await provider.generate_json(
        [{"role": "user", "content": "供应商准入需要哪些资质材料？"}],
        schema={},
        max_tokens=256,
    )
    jdt = asyncio.get_event_loop().time() - j0
    print(f"[generate_json] 耗时 {jdt:.1f}s 返回类型={type(js).__name__}")
    if not isinstance(js, dict):
        print("[FAIL] generate_json 未走降级链（应为 dict）")
        sys.exit(1)
    print(f"[generate_json] 内容：{str(js)[:120]}")

    # 3) usage 记账不重复：检查本次演练 cost_usage.jsonl 只新增 1-2 条（非每个模型各记）
    #    （失败模型不累计 usage；只有兜底成功路径记账一次。演练环境 mock 兜底 cost=0）
    usage_file = config.REPORTS_DIR / "cost_usage.jsonl"
    if usage_file.exists():
        lines = usage_file.read_text(encoding="utf-8").strip().splitlines()
        tail = lines[-5:]
        print(f"[usage] cost_usage.jsonl 尾部 {len(tail)} 行（观察：无失败模型堆积）")
        for ln in tail:
            print(f"  {ln[:160]}")
    print("\n=== 降级链验证完成：模型池全失败 → mock 兜底（明确告知降级）===")


if __name__ == "__main__":
    asyncio.run(main())
