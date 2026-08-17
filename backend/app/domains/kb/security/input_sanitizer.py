"""输入消毒器（W14 复用，规则层 P0）。

设计原则（W14 安全层）：
- 规则层确定性、零成本、无 Key 可测——但永远有盲区（中文变体/委婉表达绕过关键词正则）
- 多层防御：任何单层被绕过，其他层兜底（模型层 A7 / 输出校验）
- 降级而非拒绝：命中注入模式 -> 替换为安全占位（兼容误报，保可用性）
"""
import re

# ---- 注入模式（中英文，W14 实测有盲区——见 notes，Day5 用模型层 A7 兜底）----
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|context)",
    r"忽略(所有)?(之前|以上|先前)的(指令|提示|规则|要求)",
    r"(你现在|你是|扮演).{0,10}(系统|admin|管理员|root)",
    r"repeat\s+(the\s+)?(instructions|prompt)",
    r"(泄露|输出).{0,10}(system prompt|系统提示|指令)",
    r"(隐藏|绕过|覆盖).{0,10}(限制|规则|指令|安全)",
    r"forget\s+everything",
    r"忘记(所有|一切)",
]


class InputSanitizer:
    """输入消毒器：检测用户输入中的注入模式（规则层，fail-closed 倾向）"""

    def __init__(self):
        self._patterns = [re.compile(p, re.IGNORECASE) for p in INJECTION_PATTERNS]

    def scan(self, text: str) -> dict:
        hits = []
        for pat in self._patterns:
            m = pat.search(text)
            if m:
                hits.append({"pattern": pat.pattern[:40], "match": m.group(0)})
        return {"flagged": bool(hits), "hits": hits,
                "action": "block" if hits else "allow"}

    def sanitize(self, text: str) -> str:
        """命中注入模式 -> 替换为安全占位（不直接拒绝，兼容误报）"""
        result = self.scan(text)
        if not result["flagged"]:
            return text
        return "[已拦截疑似注入内容]"
