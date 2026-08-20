"""Embedding 封装：统一模型加载 + 向量化，供 store/retriever/评测复用。

设计要点：
- 单例模型：SentenceTransformer 加载一次，全部流程共享（进程级缓存）。
- query 与 passage 区分指令：bge 系列 query 加 QUERY_INSTRUCTION，passage 不加（官方推荐）。
- 向量统一归一化（normalize_embeddings=True），Qdrant 余弦距离下得分即为余弦相似度。
- ★ W28-D1（容器口径统一 C1）：`SCM_EMBEDDER=real|mock`（默认 real）。
  · real：加载 bge-small-zh-v1.5；加载失败（缺包/断网/模型卷未挂）→ **自动降级 mock**
    + 大写 WARNING + /health 标记 `embedder=mock_degraded`（降级哲学贯穿）。
  · mock：确定性哈希向量（512 维归一化）——零依赖、CI/无模型环境不裸奔，
    接口契约与维度与真实模型一致（语义缓存/路由/混合检索调用点无感知）。
"""

from typing import Any

import numpy as np

from app.shared import config
from app.shared.rag.model_status import record_embedder


class Embedder:
    """bge-small-zh-v1.5 封装（★ W28-D1：支持 mock 模式与自动降级）。"""

    def __init__(self, model_name: str | None = None, mode: str | None = None):
        self.model_name = model_name or config.EMBEDDING_MODEL
        self.mode = (mode or config.EMBEDDER_MODE).lower()  # real | mock
        self._model: Any = None
        self._dim: int | None = None
        # ★ W28-D1：real 加载失败时记录原因（/health 与降级判定用）
        self._load_error: str | None = None
        if self.mode == "real":
            try:
                self._load()
            except Exception as e:  # noqa: BLE001  # 降级哲学：任何加载失败 → mock，服务不崩
                self._load_error = str(e)[:200]
                print(
                    f"** WARNING ** [embedder] 真实模型加载失败，降级 mock embedder："
                    f"{type(e).__name__}: {self._load_error}"
                )
                self.mode = "mock"
                self._model = None
                self._dim = None
        record_embedder(self.status_name(), self._load_error)

    # ---------------- 状态 / 属性 ----------------

    def status_name(self) -> str:
        """/health 口径：real（真模型）/ mock（主动选择）/ mock_degraded（加载失败降级）。"""
        if self.mode == "real":
            return "real"
        return "mock_degraded" if self._load_error else "mock"

    @property
    def dim(self) -> int:
        """实际模型输出维度（real 读模型配置；mock 用配置常量 512）。"""
        if self._dim is None:
            if self.mode == "mock":
                self._dim = config.EMBEDDING_DIM
            else:
                # 直接读模型配置，不跑推理（比 encode 探测更快、更稳）
                model = self._load()
                getter = getattr(model, "get_embedding_dimension", None) or getattr(
                    model, "get_sentence_embedding_dimension", None
                )
                if getter is None:
                    raise RuntimeError(f"模型 {self.model_name} 无维度查询接口")
                self._dim = int(getter())
        return self._dim

    # ---------------- 内部加载 ----------------

    def _load(self):
        if self._model is None:
            from sentence_transformers import SentenceTransformer

            print(f"[embedder] 加载模型 {self.model_name}（首次会自动下载，后续走本地缓存）...")
            self._model = SentenceTransformer(self.model_name)
        return self._model

    # ---------------- mock 向量（确定性，维度契约与 real 一致） ----------------

    def _mock_vector(self, text: str) -> np.ndarray:
        """确定性哈希向量：md5 播种 → 512 维标准正态 → L2 归一化。

        mock 模式不承载真实语义（语义缓存/路由相似度无意义），但保证：
        1) 接口契约（返回归一化 (dim,) float32 数组）与 real 一致；
        2) 同输入同输出（复现性）；3) 零依赖零网络。
        """
        import hashlib as _hl

        seed = int(_hl.md5(text.encode("utf-8")).hexdigest()[:8], 16)
        rng = np.random.RandomState(seed)
        v = rng.randn(self.dim).astype(np.float32)
        norm = float(np.linalg.norm(v)) or 1.0
        return v / norm

    # ---------------- 对外接口 ----------------

    def embed_texts(self, texts: list[str]) -> np.ndarray:
        """passage 向量化（归一化）。返回 (N, dim) float32 数组。"""
        if self.mode == "mock":
            return (
                np.vstack([self._mock_vector(t) for t in texts])
                if texts
                else np.zeros((0, self.dim), dtype=np.float32)
            )
        model = self._load()
        return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)

    def embed_query(self, query: str) -> np.ndarray:
        """query 向量化（real 加 bge 检索指令 + 归一化）。返回 (dim,) 数组。"""
        if self.mode == "mock":
            return self._mock_vector(query)
        model = self._load()
        return model.encode(
            [config.QUERY_INSTRUCTION + query], normalize_embeddings=True, show_progress_bar=False
        )[0]

    def batch_embed(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """分批向量化（大语料用），返回按原顺序拼接的数组。"""
        if self.mode == "mock":
            return self.embed_texts(texts)
        vecs = []
        model = self._load()
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            vecs.append(model.encode(batch, normalize_embeddings=True, show_progress_bar=False))
        return np.vstack(vecs) if vecs else np.zeros((0, self.dim), dtype=np.float32)
