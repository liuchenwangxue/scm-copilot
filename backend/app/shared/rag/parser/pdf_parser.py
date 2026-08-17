"""PDF 解析器：pdfplumber 抽文本 + 表格 → 统一 Markdown（保留章节标题）。

设计（生产视角，W21 Day1）：
- 标题检测靠字号（chars.size），不依赖生成方标记：真实 PDF 从 Word/HTML 导出，
  标题 = 更大字号。行字号 > 正文基准 → 标题，按比例映射到 #/##/### 层级。
- 表格用 find_tables() 抽取（带 bbox），还原为 markdown 表格
  （供应链制度文档大量表格，表格是检索重点）。
- 文本行与表格按页面 y 坐标顺序交错合并，保持文档阅读顺序。

输出：统一 Markdown 字符串（可走项目 A 同一切块/向量化/检索链路）。

接口：
    parse_pdf(path) -> str   # 失败抛异常，由 registry 兜底记录
"""
from collections import Counter
from pathlib import Path

import pdfplumber

# 字号比例阈值（相对正文基准字号）：>=1.5 → #，>=1.25 → ##，>=1.15 → ###
HEADING_SIZE_RATIO = (1.5, 1.25, 1.15)
# 与正文字号差距过小的行不视为标题（防噪音：页眉/注释等略大字号误判）
MIN_RATIO = 1.10


def _line_height(size: float) -> float:
    """按字号估算行高（聚类时容忍行内微小 top 抖动）。"""
    return max(size * 1.4, 6.0)


def _rows_from_chars(chars: list) -> list[dict]:
    """把页面 chars 按 top 聚类成"行"：
    返回 [{top, text, size}]，size 为该行最大字号，text 按 x0 排序拼接。"""
    rows: dict[int, dict] = {}
    for c in chars:
        t = c.get("text", "")
        if not t or t.strip() == "":
            continue
        top = round(c["top"] / 2.0) * 2  # 2px 桶，容忍行内字号基线差
        if top not in rows:
            rows[top] = {"top": top, "texts": [], "size": 0.0}
        rows[top]["texts"].append((c["x0"], t))
        rows[top]["size"] = max(rows[top]["size"], float(c.get("size", 0)))
    out = []
    for r in rows.values():
        r["texts"].sort(key=lambda x: x[0])
        r["text"] = "".join(t for _, t in r["texts"]).strip()
        r.pop("texts")
        if r["text"]:
            out.append(r)
    out.sort(key=lambda r: r["top"])
    return out


def _table_to_md(table: list) -> str:
    """pdfplumber 表格（list[list[list[str]]]）→ markdown 表格。空单元格补空串。"""
    if not table:
        return ""
    rows = []
    ncols = max(len(r) for r in table)
    for r in table:
        cells = []
        for i in range(ncols):
            cell = r[i] if i < len(r) else None
            txt = " ".join(cell or [""]) if isinstance(cell, list) else str(cell or "")
            cells.append(" ".join(txt.split()).strip())
        rows.append("| " + " | ".join(cells) + " |")
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    return "\n".join([rows[0], sep] + rows[1:]) if rows else ""


def _heading_prefix(size: float, base_size: float) -> str:
    """字号 → markdown 标题前缀（无则空串，表示正文）。"""
    ratio = size / base_size if base_size else 0
    if ratio >= HEADING_SIZE_RATIO[0]:
        return "# "
    if ratio >= HEADING_SIZE_RATIO[1]:
        return "## "
    if ratio >= HEADING_SIZE_RATIO[2]:
        return "### "
    return ""


def parse_pdf(path) -> str:
    """PDF → 统一 Markdown。失败抛异常（registry 负责坏文件兜底记录）。"""
    path = Path(path)
    page_markdowns: list[str] = []

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            chars = page.chars or []
            rows = _rows_from_chars(chars)
            if not rows:
                # 扫描型/无文本层 PDF：extract_text 兜底
                txt = (page.extract_text() or "").strip()
                if txt:
                    page_markdowns.append(txt)
                continue

            # 正文基准字号 = 行字号众数（排除罕见超大字）
            sizes = [r["size"] for r in rows if r["size"] > 0]
            base_size = Counter(sizes).most_common(1)[0][0] if sizes else 11.0

            # 表格（带 bbox top，用于与文本行交错合并）
            tables = []
            try:
                for tb in page.find_tables():
                    md = _table_to_md(tb.extract())
                    if md:
                        # tb.bbox 是 (x0, top, x1, bottom) 元组，索引 1 = top
                        tables.append((tb.bbox[1], md))
            except Exception:
                tables = []

            # 合并文本行与表格，按 top 排序
            elements: list[tuple[float, str]] = []
            for r in rows:
                prefix = _heading_prefix(r["size"], base_size)
                elements.append((r["top"], prefix + r["text"]))
            elements.extend((t, m) for t, m in tables)
            elements.sort(key=lambda e: e[0])

            # 段落块：连续文本行合并，表格独立成块
            blocks: list[str] = []
            buf: list[str] = []
            for _, text in elements:
                if text.startswith("|"):
                    if buf:
                        blocks.append("\n".join(buf))
                        buf = []
                    blocks.append(text)
                else:
                    buf.append(text)
            if buf:
                blocks.append("\n".join(buf))
            page_markdowns.append("\n\n".join(b for b in blocks if b.strip()))

    return "\n\n".join(pm for pm in page_markdowns if pm.strip())
