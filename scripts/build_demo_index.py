"""Build a small FAISS index from demo_data/sample_docs/ for the public demo deployment.

Run once (needs the same deps as agent-api: sentence-transformers, faiss, langchain):
    conda activate shap_new
    python scripts/build_demo_index.py

Produces demo_data/faiss_index/{index.faiss,index.pkl} — a separate index from
data/faiss_index/ (which holds the real, private document index) so the demo
deployment never touches personal data.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from processing.chunker import chunk_text
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore

SAMPLE_DOCS_DIR = Path(__file__).resolve().parent.parent / "demo_data" / "sample_docs"
DEMO_INDEX_DIR = Path(__file__).resolve().parent.parent / "demo_data" / "faiss_index"
CHUNK_SIZE = 512
CHUNK_OVERLAP = 64


def main() -> None:
    embedder = Embedder("BAAI/bge-m3")
    store = FAISSVectorStore(embedder=embedder, dim=embedder.dim, index_dir=str(DEMO_INDEX_DIR))

    md_files = sorted(SAMPLE_DOCS_DIR.glob("*.md"))
    if not md_files:
        raise SystemExit(f"No sample docs found under {SAMPLE_DOCS_DIR}")

    total_chunks = 0
    for path in md_files:
        text = path.read_text(encoding="utf-8")
        chunks = chunk_text(text, CHUNK_SIZE, CHUNK_OVERLAP)
        if not chunks:
            continue
        docs = [
            {
                "text": chunk,
                "source": "pdf",
                "filename": path.name,
                "subject": path.stem.replace("_", " "),
                "date": date.today().isoformat(),
            }
            for chunk in chunks
        ]
        store.add(None, docs)
        total_chunks += len(docs)
        print(f"Indexed {len(docs):3d} chunks from {path.name}")

    print(f"Done. Demo index built at {DEMO_INDEX_DIR} ({total_chunks} chunks total).")


if __name__ == "__main__":
    main()
