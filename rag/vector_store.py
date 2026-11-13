import hashlib
import re
import threading
import time
from typing import Any

import numpy as np
from pathlib import Path
from datetime import datetime
from pydantic import Field
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from langchain.retrievers import EnsembleRetriever
from langchain_community.retrievers import BM25Retriever
from langchain_community.vectorstores import FAISS
from sentence_transformers import CrossEncoder

from rag.date_filter import effective_date, in_range


_RERANKER_MODEL = "jinaai/jina-reranker-v2-base-multilingual"

# Sources whose chunks are de-duplicated on ingest. Market snapshots are a time
# series, so identical text at different times is legitimate and kept.
_DEDUPE_SOURCES = {"pdf", "email"}


class _JinaReranker:
    def __init__(self, model_name: str = _RERANKER_MODEL):
        self.model_name = model_name
        self.model = CrossEncoder(model_name, trust_remote_code=True)

    def rerank(self, query: str, documents: list[dict], top_k: int = 5) -> tuple[list[dict], list[float]]:
        if not documents:
            return [], []
        pairs = [(query, doc.get("text", "")) for doc in documents]
        scores = self.model.predict(pairs, show_progress_bar=False)
        ranked = sorted(
            zip(documents, scores),
            key=lambda item: float(item[1]),
            reverse=True,
        )
        ranked_docs = [doc for doc, _ in ranked[:top_k]]
        ranked_scores = [float(score) for _, score in ranked[:top_k]]
        return ranked_docs, ranked_scores


_RERANKERS: dict[str, _JinaReranker] = {}
_RERANKERS_LOCK = threading.Lock()


def get_reranker(model_name: str = _RERANKER_MODEL) -> _JinaReranker:
    """Load the CrossEncoder once per process (~2.6s on this machine) instead of
    on every search."""
    with _RERANKERS_LOCK:
        reranker = _RERANKERS.get(model_name)
        if reranker is None:
            reranker = _RERANKERS[model_name] = _JinaReranker(model_name)
        return reranker


class _FAISSSemanticRetriever(BaseRetriever):
    vector_store: Any = Field(default=None, exclude=True)
    top_k: int = 5
    fetch_k: int = 20
    # Metadata predicate (dict -> bool). When set, plain similarity search is used with
    # fetch_k = the whole index: FAISS filters only the fetch_k nearest neighbours, so a
    # small matching set (one day's emails) would otherwise be missed entirely.
    metadata_filter: Any = Field(default=None, exclude=True)

    class Config:
        arbitrary_types_allowed = True

    def _get_relevant_documents(self, query: str, *, run_manager=None):
        if self.vector_store is None:
            return []
        if self.metadata_filter is not None:
            return self.vector_store.similarity_search(
                query, k=self.top_k, filter=self.metadata_filter, fetch_k=self.fetch_k
            )
        try:
            return self.vector_store.max_marginal_relevance_search(
                query,
                k=self.top_k,
                fetch_k=self.fetch_k,
            )
        except Exception:
            return self.vector_store.similarity_search(query, k=self.top_k)


class FAISSVectorStore:
    # Minimum seconds between "did the index files change?" stat checks.
    RELOAD_CHECK_INTERVAL_S = 5.0

    # main.py's scheduler runs the email / PDF / market ingest jobs on parallel
    # threads; serialise their index writes (process-wide, shared by instances).
    _write_lock = threading.RLock()

    def __init__(
        self,
        embedder,
        dim: int | None = None,
        index_dir: str = "data/faiss_index",
        auto_reload: bool = False,
    ):
        """auto_reload: for read-only consumers (the agent API) of an index that
        another process (main.py) keeps writing. The index is re-read from disk
        whenever its files change. Leave off for the writer: it holds the
        authoritative in-memory copy and saves after every add."""
        # dim 参数保留仅为兼容旧调用方。
        self.dim = dim
        self.embedder = embedder
        self.index_dir = Path(index_dir)
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.index_path = self.index_dir / "index.faiss"
        self.pkl_path = self.index_dir / "index.pkl"
        self._vs = None
        self._auto_reload = auto_reload
        self._reload_lock = threading.Lock()
        self._loaded_sig = None
        self._next_reload_check = 0.0

        if self.index_path.exists() and self.pkl_path.exists():
            self._loaded_sig = self._index_signature()
            self._vs = self._load_index()

    @property
    def vs(self):
        if getattr(self, "_auto_reload", False):
            self._reload_if_changed()
        return getattr(self, "_vs", None)

    @vs.setter
    def vs(self, value):
        self._vs = value

    def _index_signature(self):
        """(mtime_ns, size) of both index files, or None if either is missing."""
        try:
            return tuple(
                (st.st_mtime_ns, st.st_size)
                for st in (self.index_path.stat(), self.pkl_path.stat())
            )
        except OSError:
            return None

    def _load_index(self):
        return FAISS.load_local(
            str(self.index_dir),
            self.embedder.lc_embeddings,
            allow_dangerous_deserialization=True,
        )

    def _reload_if_changed(self) -> None:
        """Swap in a fresh copy of the on-disk index if the writer updated it.

        The writer saves index.faiss then index.pkl, not atomically, so a read can
        land mid-save. A copy is only adopted if the files did not change while it
        loaded and the vector count matches the docstore; otherwise the current
        copy is kept and the next check retries."""
        now = time.monotonic()
        if now < self._next_reload_check:
            return
        if not self._reload_lock.acquire(blocking=False):
            return
        try:
            if now < self._next_reload_check:
                return
            self._next_reload_check = now + self.RELOAD_CHECK_INTERVAL_S
            sig = self._index_signature()
            if sig is None or sig == self._loaded_sig:
                return
            try:
                fresh = self._load_index()
                consistent = fresh.index.ntotal == len(fresh.index_to_docstore_id)
            except Exception as e:
                print(f"[reload] index changed but could not be read yet, keeping current copy: {e}")
                return
            if not consistent or self._index_signature() != sig:
                print("[reload] index was being written during load, will retry")
                return
            old = getattr(self._vs, "index", None)
            self._vs = fresh
            self._loaded_sig = sig
            self._bm25_cache = None
            self._content_keys = None
            print(
                f"[reload] index reloaded: {getattr(old, 'ntotal', 0)} -> {fresh.index.ntotal} vectors"
            )
        finally:
            self._reload_lock.release()

    @staticmethod
    def _content_key(metadata: dict, text: str):
        """Identity of a chunk for de-duplication: (owning document, text hash).

        A PDF is identified by filename (so the same file arriving as an email
        attachment and as a local upload collapses); an email by subject + date.
        The same text in two *different* documents (e.g. a shared disclaimer) is
        not a duplicate. Returns None for sources that are not de-duplicated.
        """
        if metadata.get("source") not in _DEDUPE_SOURCES:
            return None
        if metadata.get("filename"):
            owner = ("file", metadata["filename"])
        else:
            owner = ("mail", metadata.get("subject", ""), metadata.get("date", ""))
        return owner, hashlib.sha1(text.strip().encode("utf-8", errors="ignore")).hexdigest()

    def _existing_content_keys(self) -> set:
        # getattr: some tests build a store without running __init__.
        keys = getattr(self, "_content_keys", None)
        if keys is None:
            keys = set()
            if self.vs is not None:
                for doc in self.vs.docstore._dict.values():
                    key = self._content_key(getattr(doc, "metadata", {}) or {}, doc.page_content)
                    if key:
                        keys.add(key)
            self._content_keys = keys
        return keys

    def add(self, embeddings: np.ndarray | None, docs: list[dict]) -> int:
        with self._write_lock:
            return self._add_locked(embeddings, docs)

    def _add_locked(self, embeddings: np.ndarray | None, docs: list[dict]) -> int:
        """Index chunks, skipping ones already present for the same document.

        Returns the number of chunks actually added.
        """
        # embeddings 参数保留仅为兼容旧调用方。
        if not docs:
            return 0

        existing = self._existing_content_keys()
        seen: set = set()
        kept: list[dict] = []
        for doc in docs:
            key = self._content_key(doc, doc.get("text", ""))
            if key and (key in existing or key in seen):
                continue
            if key:
                seen.add(key)
            kept.append(doc)
        if len(kept) < len(docs):
            print(f"[dedupe] skipped {len(docs) - len(kept)} duplicate chunk(s) of {len(docs)}")
        if not kept:
            return 0
        docs = kept

        texts = [doc.get("text", "") for doc in docs]
        metadatas = [{k: v for k, v in doc.items() if k != "text"} for doc in docs]

        if self.vs is None:
            self.vs = FAISS.from_texts(texts, self.embedder.lc_embeddings, metadatas=metadatas)
        else:
            self.vs.add_texts(texts=texts, metadatas=metadatas)
        existing |= seen
        self._save()
        return len(docs)

    def search(self, query_embedding: np.ndarray, top_k: int = 5):
        if self.vs is None:
            return []

        if query_embedding.ndim == 2:
            vector = query_embedding[0].astype("float32").tolist()
        else:
            vector = query_embedding.astype("float32").tolist()

        docs = self.vs.similarity_search_by_vector(vector, k=top_k)
        return [{"text": d.page_content, **d.metadata} for d in docs]

    def get_langchain_documents(self) -> list[Document]:
        """Return all indexed chunks as LangChain Documents for hybrid retrieval."""
        if self.vs is None:
            return []

        documents: list[Document] = []
        docstore = getattr(self.vs, "docstore", None)
        if docstore is None:
            return documents

        for _, doc_id in getattr(self.vs, "index_to_docstore_id", {}).items():
            try:
                if hasattr(docstore, "search"):
                    doc = docstore.search(doc_id)
                else:
                    doc = docstore._dict.get(doc_id)
            except Exception:
                doc = None

            if doc is None:
                continue
            metadata = dict(getattr(doc, "metadata", {}) or {})
            documents.append(Document(page_content=doc.page_content, metadata=metadata))

        if not documents:
            try:
                for doc in getattr(docstore, "_dict", {}).values():
                    metadata = dict(getattr(doc, "metadata", {}) or {})
                    documents.append(Document(page_content=doc.page_content, metadata=metadata))
            except Exception:
                pass

        return documents

    def _get_bm25(self, k: int) -> BM25Retriever | None:
        """BM25 over every indexed chunk, built once and reused (rebuilding it
        per search cost ~0.4s at 11k chunks). Keyed on the live FAISS object and
        its size, and dropped by _save(), so adds/deletes are picked up."""
        index = getattr(self.vs, "index", None)
        key = (id(self.vs), getattr(index, "ntotal", None))
        cached = getattr(self, "_bm25_cache", None)
        if cached is None or cached[0] != key:
            docs = self.get_langchain_documents()
            if not docs:
                return None
            cached = self._bm25_cache = (key, BM25Retriever.from_documents(docs))
        bm25 = cached[1]
        bm25.k = k
        return bm25

    def warmup(self) -> None:
        """Pay the one-time costs (BM25 build, reranker load) now rather than
        inside the first user question."""
        if self.vs is None:
            return
        self._get_bm25(8)
        get_reranker()

    def search_text(self, query: str, top_k: int = 5, date_range: tuple[str, str] | None = None):
        docs, _ = self.search_text_with_scores(query, top_k=top_k, date_range=date_range)
        return docs

    def latest_document_date(self) -> str:
        """Newest ISO date of any indexed email or PDF ('' if none is dated)."""
        if self.vs is None:
            return ""
        dates = (
            effective_date(getattr(doc, "metadata", None))
            for doc in self.vs.docstore._dict.values()
            if (getattr(doc, "metadata", None) or {}).get("source") in ("email", "pdf")
        )
        return max((d for d in dates if d), default="")

    def search_text_with_scores(self, query: str, top_k: int = 5, date_range: tuple[str, str] | None = None):
        """Returns (docs, best_score) where best_score ∈ [0, 1], 1 = perfect match.

        Combines a keyword retriever (BM25) with a semantic vector retriever (FAISS)
        through LangChain's EnsembleRetriever for a lightweight hybrid search,
        then reranks the union with a Jina reranker and removes near-duplicates.

        date_range=(start, end) as ISO dates restricts the search to chunks dated in that
        range (email date, or the date in a PDF's file name; see rag/date_filter.py). The
        restriction is applied to both retrievers *before* ranking, so the top results are
        the best of that day, not whatever of the overall top candidates happens to match.
        Chunks without any date are excluded. No match gives ([], 0.0).
        """
        if self.vs is None:
            return [], 0.0

        candidate_k = max(top_k, 8)
        metadata_filter = None
        if date_range:
            start, end = date_range
            metadata_filter = lambda md: in_range(md, start, end)  # noqa: E731
            pool = [d for d in self.get_langchain_documents() if metadata_filter(d.metadata)]
            if not pool:
                return [], 0.0
            ntotal = getattr(getattr(self.vs, "index", None), "ntotal", None) or len(self.vs.docstore._dict)
            scored = self.vs.similarity_search_with_relevance_scores(
                query, k=1, filter=metadata_filter, fetch_k=ntotal
            )
            bm25_retriever = BM25Retriever.from_documents(pool)  # small pool: built per search
            bm25_retriever.k = candidate_k
            fetch_k = ntotal
        else:
            scored = self.vs.similarity_search_with_relevance_scores(query, k=1)
            bm25_retriever = self._get_bm25(candidate_k)
            fetch_k = max(candidate_k * 4, 20)
        best_score = scored[0][1] if scored else 0.0

        if bm25_retriever is None:
            return [], best_score

        vector_retriever = _FAISSSemanticRetriever(
            vector_store=self.vs,
            top_k=candidate_k,
            fetch_k=fetch_k,
            metadata_filter=metadata_filter,
        )

        ensemble_retriever = EnsembleRetriever(
            retrievers=[bm25_retriever, vector_retriever],
            weights=[0.5, 0.5],
        )
        results = ensemble_retriever.get_relevant_documents(query)
        merged_docs = [{"text": d.page_content, **d.metadata} for d in results]

        merged_docs = self._deduplicate_documents(merged_docs)

        reranker = get_reranker()
        reranked_docs, rerank_scores = reranker.rerank(query, merged_docs, top_k=min(top_k, 3))
        for doc, score in zip(reranked_docs, rerank_scores):
            doc["rerank_score"] = round(float(score), 4)
            doc["rerank_source"] = "jina-reranker-v2-base-multilingual"

        if reranked_docs:
            self._print_rerank_report(query, reranked_docs)
            return reranked_docs, best_score
        return merged_docs, best_score

    @staticmethod
    def _deduplicate_documents(documents: list[dict]) -> list[dict]:
        seen: set[str] = set()
        deduped: list[dict] = []
        for doc in documents:
            text = (doc.get("text") or "").strip()
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(doc)
        return deduped

    @staticmethod
    def _print_rerank_report(query: str, docs: list[dict]) -> None:
        lines = [f"\n[Hybrid Rerank] query={query}"]
        lines.append("-" * 96)
        lines.append(f"{'#':<2} {'score':<7} {'source':<10} {'file':<20} {'preview'}")
        lines.append("-" * 96)
        for idx, doc in enumerate(docs, 1):
            source = str(doc.get("source", "unknown"))
            filename = str(doc.get("filename", ""))
            preview = (doc.get("text", "") or "").replace("\n", " ")[:80]
            lines.append(f"{idx:<2} {doc.get('rerank_score', 0.0):<7.4f} {source:<10} {filename:<20} {preview}")
        lines.append("-" * 96)

        report_text = "\n".join(lines)
        print(report_text)

        try:
            log_dir = Path("data") / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_path = log_dir / "rerank_report.log"
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"\n[{datetime.now().isoformat()}]\n")
                handle.write(report_text + "\n")
        except Exception:
            pass

    def search_by_filename(self, filename_query: str, top_k: int = 5):
        """Find documents by PDF filename metadata (exact/partial match)."""
        if self.vs is None:
            return []

        q = (filename_query or "").strip().lower()
        if not q:
            return []

        results: list[tuple[int, dict]] = []
        for _, doc_id in self.vs.index_to_docstore_id.items():
            doc = self.vs.docstore.search(doc_id)
            if doc is None:
                continue
            metadata = getattr(doc, "metadata", {}) or {}
            filename = str(metadata.get("filename", "")).strip()
            if not filename:
                continue

            filename_l = filename.lower()
            score = 0
            if q == filename_l:
                score = 3
            elif q in filename_l:
                score = 2
            elif filename_l in q:
                score = 1

            if score > 0:
                results.append((score, {"text": doc.page_content, **metadata}))

        results.sort(key=lambda x: x[0], reverse=True)
        return [item[1] for item in results[:top_k]]

    def get_chunks_by_filename(self, filename: str) -> list[dict]:
        """All indexed chunks of one PDF, in ingestion (= document) order.

        Exact case-insensitive filename match; falls back to a partial match only
        when it points at a single distinct file. Returns [] if nothing (or an
        ambiguous set of files) matches.
        """
        if self.vs is None:
            return []
        q = (filename or "").strip().lower()
        if not q:
            return []

        by_name: dict[str, list[dict]] = {}
        for _, doc_id in self.vs.index_to_docstore_id.items():
            doc = self.vs.docstore.search(doc_id)
            if doc is None:
                continue
            metadata = getattr(doc, "metadata", {}) or {}
            name = str(metadata.get("filename", "")).strip()
            if name:
                by_name.setdefault(name.lower(), []).append({"text": doc.page_content, **metadata})

        if q in by_name:
            return by_name[q]
        partial = [n for n in by_name if q in n or n in q]
        return by_name[partial[0]] if len(partial) == 1 else []

    def get_chunks_by_subject(self, subject: str) -> list[dict]:
        """All indexed chunks of one email, in ingestion (= body) order.

        Exact case-insensitive subject match (a leading "Fwd:"/"Re:" is
        ignored); falls back to a partial match only when it points at a single
        distinct subject. If the same subject was indexed on several dates, the
        newest date wins. Returns [] if nothing (or an ambiguous set) matches.
        """
        if self.vs is None:
            return []

        def norm(s: str) -> str:
            return re.sub(r"^(?:(?:fwd?|re|aw|wg)\s*:\s*)+", "", s.strip().lower())

        q = norm(subject or "")
        if not q:
            return []

        by_key: dict[tuple[str, str], list[dict]] = {}
        for _, doc_id in self.vs.index_to_docstore_id.items():
            doc = self.vs.docstore.search(doc_id)
            if doc is None:
                continue
            metadata = getattr(doc, "metadata", {}) or {}
            if metadata.get("source") != "email":
                continue
            subj = norm(str(metadata.get("subject", "")))
            if subj:
                key = (subj, str(metadata.get("date") or ""))
                by_key.setdefault(key, []).append({"text": doc.page_content, **metadata})

        exact = [k for k in by_key if k[0] == q]
        if not exact:
            partial_subjects = {k[0] for k in by_key if q in k[0]}
            if len(partial_subjects) != 1:
                return []
            exact = [k for k in by_key if k[0] in partial_subjects]
        return by_key[max(exact, key=lambda k: k[1])]

    def _save(self):
        self._bm25_cache = None
        if self.vs is not None:
            self.vs.save_local(str(self.index_dir))

    def delete_by_metadata(self, filters: dict[str, str]) -> int:
        with self._write_lock:
            return self._delete_by_metadata_locked(filters)

    def _delete_by_metadata_locked(self, filters: dict[str, str]) -> int:
        """Delete chunks whose metadata matches all provided key/value pairs.

        Returns the number of deleted chunks.
        """
        if self.vs is None or not filters:
            return 0

        ids_to_delete: list[str] = []
        for _, doc_id in getattr(self.vs, "index_to_docstore_id", {}).items():
            doc = self.vs.docstore.search(doc_id)
            if doc is None:
                continue
            metadata = getattr(doc, "metadata", {}) or {}
            if all(str(metadata.get(k, "")) == str(v) for k, v in filters.items()):
                ids_to_delete.append(doc_id)

        if not ids_to_delete:
            return 0

        self.vs.delete(ids=ids_to_delete)
        self._content_keys = None  # rebuilt lazily; deleted chunks may be re-added
        self._save()
        return len(ids_to_delete)
# 1. MMR retrieval (vector_store.py) — replaced similarity_search with max_marginal_relevance_search(k=top_k, fetch_k=max(top_k*4, 20)). 
# It oversamples 20+ candidates then selects the top_k that are both relevant AND diverse, so near-duplicate email chunks won't both land in the results.