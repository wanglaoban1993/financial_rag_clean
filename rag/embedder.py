import logging
import numpy as np

logger = logging.getLogger(__name__)

try:
    from langchain_huggingface import HuggingFaceEmbeddings
except ModuleNotFoundError:
    from langchain_community.embeddings import HuggingFaceEmbeddings


class Embedder:
    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",#         Multilingual, strong in both Chinese and English, supports long texts
        query_prefix: str | None = None,
        normalize_embeddings: bool = True,
    ):
        """
        Recommended models:
        - BAAI/bge-m3         Multilingual, strong in both Chinese and English, supports long texts
        - BAAI/bge-large-zh   Focused on Chinese, fast
        """
        self.model_name = model_name
        self.query_prefix = query_prefix
        logger.info("Loading embedding model: %s", model_name)

        self.model = HuggingFaceEmbeddings(
            model_name=model_name,
            encode_kwargs={"normalize_embeddings": normalize_embeddings},
        )

        self.dim = len(self.model.embed_query("dimension detection test"))
        logger.info("Vector dimension: %s", self.dim)

    def encode(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.empty((0, self.dim), dtype=np.float32)

        embeddings = self.model.embed_documents(texts)
        return np.asarray(embeddings, dtype=np.float32)

    def encode_query(self, query: str) -> np.ndarray:
        text = f"{self.query_prefix}{query}" if self.query_prefix else query
        return self.encode([text])[0]  # return to 1D vector

    @property
    def lc_embeddings(self):
        return self.model