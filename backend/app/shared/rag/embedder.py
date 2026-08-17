"""Embedding 封装：统一模型加载 + 向量化，供 store/retriever/评测复用。

设计要点：
- 单例模型：SentenceTransformer 加载一次，全部流程共享（进程级缓存）。
- query 与 passage 区分指令：bge 系列 query 加 QUERY_INSTRUCTION，passage 不加（官方推荐）。
- 向量统一归一化（normalize_embeddings=True），Qdrant 余弦距离下得分即为余弦相似度。
"""
from typing import Any

import numpy as np

from app.shared import config


class Embedder:
    """bge-small-zh-v1.5 封装。"""

    def __init__(self, model_name: str | None = None):
        self.model_name = model_name or config.EMBEDDING_MODEL
        self._model: Any = None
        self._dim: int | None = None

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer
            print(f"[embedder] 加载模型 {self.model_name}（首次会自动下载，后续走本地缓存）...")
            self._model = SentenceTransformer(self.model_name)
        return self._model

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """passage 向量化（归一化）。返回 (N, dim) float32 数组。"""
        model = self._load()
        return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    def embed_query(self, query: str) -> np.ndarray:
        """query 向量化（加 bge 检索指令 + 归一化）。返回 (dim,) 数组。"""
        model = self._load()
        return model.encode(
            [config.QUERY_INSTRUCTION + query], normalize_embeddings=True,
            show_progress_bar=False)[0]

    @property
    def dim(self) -> int:
        """实际模型输出维度（惰性缓存，首次访问时探测）。"""
        if self._dim is None:
            # 直接读模型配置，不跑推理（比 encode 探测更快、更稳）
            model = self._load()
            getter = getattr(model, "get_embedding_dimension", None) \
                or getattr(model, "get_sentence_embedding_dimension", None)
            if getter is None:
                raise RuntimeError(f"模型 {self.model_name} 无维度查询接口")
            self._dim = int(getter())
        return self._dim

    def batch_embed(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """分批向量化（大语料用），返回按原顺序拼接的数组。"""
        import numpy as _np
        vecs = []
        model = self._load()
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            vecs.append(model.encode(batch, normalize_embeddings=True, show_progress_bar=False))
        return _np.vstack(vecs) if vecs else _np.zeros((0, self.dim), dtype=_np.float32)
