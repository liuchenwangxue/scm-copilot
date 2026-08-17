"""多格式解析注册表：按扩展名路由解析器，坏文件跳过 + 记录（W21 Day1）。

支持格式：.md（原样读取，保留标题层级）/ .pdf（pdfplumber 字号检测标题）/ .docx（python-docx 样式识别）。

接口：
    SUPPORTED_EXT : set[str]
    doc_id_from_filename(name) -> str          # "SCM-PUR-001_xx.pdf" → "SCM-PUR-001_xx"
    parse_document(path) -> dict               # {file, doc_id, ext, markdown, ok, error}
    parse_document_batch(paths) -> dict        # 批量解析 + 坏文件统计（带兜底记录）
"""
from pathlib import Path

from app.shared.rag.parser.pdf_parser import parse_pdf
from app.shared.rag.parser.word_parser import parse_docx

SUPPORTED_EXT = {".md", ".pdf", ".docx"}


def doc_id_from_filename(name: str) -> str:
    """文件名 → doc_id：去掉扩展名（保持与现有 MD 文档 doc_id 一致）。"""
    return Path(name).name.rsplit(".", 1)[0] if "." in Path(name).name else Path(name).name


def parse_markdown(path: Path) -> str:
    """.md 原样读取（已含标题层级，无需加工）。"""
    return path.read_text(encoding="utf-8")


def parse_document(path) -> dict:
    """解析单个文件：按扩展名路由。坏文件返回 ok=False + error（不抛异常）。

    返回:
        {file, doc_id, ext, markdown, ok, error}
    """
    path = Path(path)
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            markdown = parse_pdf(path)
        elif ext == ".docx":
            markdown = parse_docx(path)
        elif ext == ".md":
            markdown = parse_markdown(path)
        else:
            return {"file": str(path), "doc_id": doc_id_from_filename(path.name),
                    "ext": ext, "markdown": "", "ok": False,
                    "error": f"不支持的格式 {ext}（支持：{sorted(SUPPORTED_EXT)}）"}
        if not markdown.strip():
            return {"file": str(path), "doc_id": doc_id_from_filename(path.name),
                    "ext": ext, "markdown": "", "ok": False, "error": "解析结果为空"}
        return {"file": str(path), "doc_id": doc_id_from_filename(path.name),
                "ext": ext, "markdown": markdown, "ok": True, "error": ""}
    except Exception as e:  # 坏文件兜底：跳过 + 记录，不让整批解析崩溃
        return {"file": str(path), "doc_id": doc_id_from_filename(path.name),
                "ext": ext, "markdown": "", "ok": False,
                "error": f"{type(e).__name__}: {str(e)[:150]}"}


def parse_document_batch(paths: list, errors_log: Path | None = None) -> dict:
    """批量解析：返回 {documents, ok_count, bad_files}。
    errors_log 非空时把坏文件记录追加写入（JSON line）。"""
    import json
    import time

    documents = [parse_document(p) for p in paths]
    bad = [d for d in documents if not d["ok"]]
    if bad and errors_log is not None:
        with open(errors_log, "a", encoding="utf-8") as f:
            for d in bad:
                f.write(json.dumps({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "file": d["file"], "doc_id": d["doc_id"], "ext": d["ext"],
                    "error": d["error"],
                }, ensure_ascii=False) + "\n")
    return {"documents": documents, "ok_count": len(documents) - len(bad),
            "bad_files": bad}
