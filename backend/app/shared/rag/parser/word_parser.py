"""Word 解析器：python-docx 抽段落 + 表格 → 统一 Markdown。

设计（生产视角，W21 Day1）：
- 标题识别靠段落 style（Heading 1/2/3，兼容中文"标题 1/2/3"）——真实 Word 制度文档
  标题都走样式，不靠字号猜。
- 按 body 子节点顺序遍历（paragraph / table 交错），保持文档阅读顺序。
- 表格（w:tbl）→ markdown 表格（表头=首行，供应链表格是检索重点）。

接口：
    parse_docx(path) -> str   # 失败抛异常，由 registry 兜底记录
"""
# 标题样式名 → markdown 前缀（兼容 python-docx 英文默认模板 / 中文模板）
_HEADING_MAP = {
    "heading 1": "# ", "标题 1": "# ", "heading 2": "## ", "标题 2": "## ",
    "heading 3": "### ", "标题 3": "### ",
}


def _table_to_md(table) -> str:
    """python-docx Table → markdown 表格。cell 文本取去空白后的单行。"""
    rows = []
    ncols = len(table.columns)
    for row in table.rows:
        cells = []
        for i in range(ncols):
            cell = row.cells[i].text if i < len(row.cells) else ""
            cells.append(" ".join(cell.split()).strip())
        rows.append("| " + " | ".join(cells) + " |")
    sep = "| " + " | ".join(["---"] * ncols) + " |"
    return "\n".join([rows[0], sep] + rows[1:]) if rows else ""


def parse_docx(path) -> str:
    """docx → 统一 Markdown。失败抛异常（registry 负责坏文件兜底记录）。"""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    parts: list[str] = []

    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            text = p.text.strip()
            if not text:
                continue
            style = (p.style.name if p.style is not None else "") or ""
            prefix = _HEADING_MAP.get(style.strip().lower(), "")
            parts.append(prefix + text)
        elif child.tag == qn("w:tbl"):
            t = Table(child, doc)
            md = _table_to_md(t)
            if md:
                parts.append(md)

    return "\n\n".join(parts)
