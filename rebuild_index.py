import argparse
import hashlib
import json
import shutil
import time
from pathlib import Path

import yaml
from config_loader import load_config, resolve_config_path

from ingestion.email_fetcher import QQEmailFetcher
from ingestion.market_fetcher import MarketFetcher
from processing.chunker import chunk_markdown, chunk_parsed_pdf
from processing.pdf_parser import parse_pdf
from rag.embedder import Embedder
from rag.pdf_watch import resolve_pdf_scan_root
from rag.vector_store import FAISSVectorStore


def _delete_if_exists(path: Path) -> bool:
    if path.exists():
        path.unlink()
        return True
    return False


def _reset_state(index_dir: Path, reset_processed_logs: bool = True) -> None:
    deleted = []
    for name in ("index.faiss", "index.pkl"):
        p = index_dir / name
        if _delete_if_exists(p):
            deleted.append(str(p))

    if reset_processed_logs:
        for p in (
            Path("data/processed_pdfs.txt"),
            Path("data/processed_emails.txt"),
            Path("data/processed_local_pdfs.json"),
        ):
            if _delete_if_exists(p):
                deleted.append(str(p))

    if deleted:
        print("Deleted old state:")
        for item in deleted:
            print(f"  - {item}")
    else:
        print("No old state files found.")


def _looks_meaningful_email_text(text: str) -> bool:
    if not text:
        return False
    alpha_count = sum(ch.isalpha() for ch in text)
    return alpha_count >= 40


def _backup_index(index_dir: Path) -> None:
    """Keep the old index so a bad rebuild can be rolled back."""
    if not any((index_dir / n).exists() for n in ("index.faiss", "index.pkl")):
        return
    backup = index_dir.with_name(f"{index_dir.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copytree(index_dir, backup)
    print(f"Backed up old index to {backup}")


def _pdf_signature(path: Path) -> str:
    # Same format as main.py, so main.py sees these files as already indexed.
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _email_key(doc: dict) -> str:
    # Same key as main.py's _build_email_key.
    message_id = (doc.get("message_id") or "").strip()
    if message_id:
        return message_id
    raw = f"{doc.get('subject', '')}|{doc.get('body', '')[:2000]}"
    return "fallback:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def rebuild(
    cfg: dict,
    index_dir: str,
    pdf_dir: str,
    include_local_pdfs: bool,
    include_emails: bool,
    include_market: bool,
) -> None:
    index_path = Path(index_dir)
    index_path.mkdir(parents=True, exist_ok=True)
    _backup_index(index_path)

    _reset_state(index_path, reset_processed_logs=True)

    embedder = Embedder(cfg["rag"]["embedding_model"])
    store = FAISSVectorStore(embedder=embedder, dim=embedder.dim, index_dir=index_dir)

    chunk_size = cfg["rag"]["chunk_size"]
    chunk_overlap = cfg["rag"]["chunk_overlap"]

    total_chunks = 0

    if include_local_pdfs:
        root = Path(pdf_dir)
        pdf_files = sorted(root.rglob("*.pdf")) if root.exists() else []
        print(f"Local PDF files: {len(pdf_files)}")
        pdf_registry: dict[str, str] = {}
        for pdf_path in pdf_files:
            try:
                parsed = parse_pdf(str(pdf_path.resolve()), backend="docling")
                chunks = chunk_parsed_pdf(parsed, chunk_size, chunk_overlap)
                if chunks:
                    docs = [
                        {
                            "text": c.text,
                            "source": "pdf",
                            "filename": pdf_path.name,
                            "source_path": str(pdf_path.resolve()),
                            "heading": c.heading_path,
                            "has_table": c.has_table,
                            **({"page": c.page} if c.page else {}),
                        }
                        for c in chunks
                    ]
                    store.add(None, docs)
                    total_chunks += len(chunks)
                pdf_registry[str(pdf_path.resolve())] = _pdf_signature(pdf_path)
                print(f"  parsed: {pdf_path.name} chunks={len(chunks)}")
            except Exception as exc:
                print(f"  [WARN] local pdf parse failed: {pdf_path} - {exc}")

        # main.py's registry: without it the next scan would index every PDF a second time.
        registry_path = Path("data/processed_local_pdfs.json")
        registry_path.parent.mkdir(parents=True, exist_ok=True)
        registry_path.write_text(
            json.dumps(pdf_registry, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
        )

    if include_emails:
        # The service marks mail as read once ingested, so a rebuild needs the read ones too.
        fetcher = QQEmailFetcher({**cfg["email"], "include_seen": True})
        emails = fetcher.fetch_new_emails()
        print(f"Fetched emails: {len(emails)}")
        for doc in emails:
            body = (doc.get("body") or "").strip()
            body_chunks = []
            if _looks_meaningful_email_text(body):
                body_chunks = chunk_markdown(body, chunk_size, chunk_overlap)

            if body_chunks:
                email_docs = [
                    {
                        "text": c.text,
                        "source": "email",
                        "subject": doc.get("subject", ""),
                        "date": doc.get("date", ""),
                        "newsletter_path": doc.get("newsletter_path", ""),
                        "heading": c.heading_path,
                    }
                    for c in body_chunks
                ]
                store.add(None, email_docs)
                total_chunks += len(body_chunks)

            with Path("data/processed_emails.txt").open("a", encoding="utf-8") as f:
                f.write(_email_key(doc) + "\n")

            pdf_paths = doc.get("pdf_paths", [])
            for path in pdf_paths:
                try:
                    parsed = parse_pdf(path, backend="docling")
                    pdf_chunks = chunk_parsed_pdf(parsed, chunk_size, chunk_overlap)
                    if pdf_chunks:
                        pdf_docs = [
                            {
                                "text": c.text,
                                "source": "pdf",
                                "filename": Path(path).name,
                                "subject": doc.get("subject", ""),
                                "date": doc.get("date", ""),
                                "heading": c.heading_path,
                                "has_table": c.has_table,
                                **({"page": c.page} if c.page else {}),
                            }
                            for c in pdf_chunks
                        ]
                        store.add(None, pdf_docs)
                        total_chunks += len(pdf_chunks)
                except Exception as exc:
                    print(f"  [WARN] email pdf parse failed: {path} - {exc}")

    if include_market:
        fetcher = MarketFetcher(cfg["market"]["symbols"])
        market_docs = fetcher.fetch()
        print(f"Market records: {len(market_docs)}")
        for doc in market_docs:
            chunks = chunk_markdown(doc["text"], chunk_size, chunk_overlap)
            if chunks:
                payload = [
                    {
                        "text": c.text,
                        "source": "market",
                        "symbol": doc["symbol"],
                        "timestamp": doc["timestamp"],
                    }
                    for c in chunks
                ]
                store.add(None, payload)
                total_chunks += len(chunks)

    total_vectors = store.vs.index.ntotal if store.vs is not None else 0
    print("Rebuild finished.")
    print(f"Total chunks added: {total_chunks}")
    print(f"Total vectors in index: {total_vectors}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset and rebuild FAISS index from configured data sources.")
    parser.add_argument(
        "--config",
        default=str(resolve_config_path()),
        help="Path to config YAML (default: FIN_RAG_CONFIG or resolved secure path)",
    )
    parser.add_argument("--index-dir", default="data/faiss_index", help="FAISS index directory")
    parser.add_argument("--pdf-dir", default=None, help="Local PDF directory (default: rag.pdf_scan_root from config)")
    parser.add_argument("--skip-local-pdf", action="store_true", help="Skip local PDF ingestion")
    parser.add_argument("--skip-email", action="store_true", help="Skip email ingestion")
    parser.add_argument("--skip-market", action="store_true", help="Skip market data ingestion")
    args = parser.parse_args()

    if args.config == str(resolve_config_path()):
        cfg = load_config(strict=True)
    else:
        with open(args.config, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

    rebuild(
        cfg=cfg,
        index_dir=args.index_dir,
        pdf_dir=args.pdf_dir or str(resolve_pdf_scan_root(cfg)),
        include_local_pdfs=not args.skip_local_pdf,
        include_emails=not args.skip_email,
        include_market=not args.skip_market,
    )


if __name__ == "__main__":
    main()
