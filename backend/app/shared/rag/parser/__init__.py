"""多格式解析包（W21 Day1）：PDF/Word/MD → 统一 Markdown，接入项目 A 同一切块/向量化/检索链路。"""
from .registry import (  # noqa: F401
    SUPPORTED_EXT,
    doc_id_from_filename,
    parse_document,
    parse_document_batch,
)
