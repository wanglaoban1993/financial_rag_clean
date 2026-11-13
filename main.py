import re
import hashlib
import time
import json
import sys
from importlib import metadata
from pathlib import Path
from apscheduler.schedulers.blocking import BlockingScheduler
from ingestion.email_fetcher import QQEmailFetcher
from ingestion.market_fetcher import MarketFetcher
from processing.pdf_parser import parse_pdf
from processing.chunker import chunk_text, chunk_parsed_pdf
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from rag.pdf_watch import find_new_pdf_files, resolve_pdf_scan_root
from rag.schedule_utils import parse_check_times
from config_loader import load_config, resolve_config_path

cfg = load_config(strict=True)


def _safe_pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "not-installed"


def _print_startup_env_check():
    print("=" * 72)
    print("Startup Environment Check")
    print("=" * 72)
    print(f"Python executable: {sys.executable}")
    print(f"Python version: {sys.version.split()[0]}")
    print(f"Config path: {resolve_config_path()}")

    key_packages = ["docling", "transformers", "torch", "torchvision", "playwright"]
    versions = {name: _safe_pkg_version(name) for name in key_packages}
    for name in key_packages:
        print(f"{name}: {versions[name]}")

    if "/envs/rag/" not in sys.executable:
        print("[WARN] You are not using the rag conda environment interpreter.")
        print("       Recommended: conda activate rag")
    if versions["docling"] == "not-installed":
        print("[WARN] docling is not installed in this interpreter.")
    if versions["playwright"] == "not-installed":
        print("[WARN] playwright is not installed in this interpreter.")
    print("=" * 72)


_print_startup_env_check()

embedder = Embedder(cfg["rag"]["embedding_model"])
store = FAISSVectorStore(embedder=embedder, dim=embedder.dim)

email_fetcher = QQEmailFetcher(cfg["email"])
market_fetcher = MarketFetcher(cfg["market"]["symbols"], cfg.get("market", {}))

processed_pdf_log = Path("data/processed_pdfs.txt")
processed_email_log = Path("data/processed_emails.txt")
processed_local_pdf_state = Path("data/processed_local_pdfs.json")


def _load_processed_pdf_paths() -> set[str]:
    if not processed_pdf_log.exists():
        return set()
    return {line.strip() for line in processed_pdf_log.read_text(encoding="utf-8").splitlines() if line.strip()}


def _append_processed_pdf_path(path: str):
    processed_pdf_log.parent.mkdir(parents=True, exist_ok=True)
    with processed_pdf_log.open("a", encoding="utf-8") as f:
        f.write(path + "\n")


def _load_processed_email_ids() -> set[str]:
    if not processed_email_log.exists():
        return set()
    return {
        line.strip() for line in processed_email_log.read_text(encoding="utf-8").splitlines() if line.strip()
    }


def _append_processed_email_id(email_id: str):
    processed_email_log.parent.mkdir(parents=True, exist_ok=True)
    with processed_email_log.open("a", encoding="utf-8") as f:
        f.write(email_id + "\n")


def _load_local_pdf_state() -> dict[str, str]:
    if not processed_local_pdf_state.exists():
        return {}
    try:
        data = json.loads(processed_local_pdf_state.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_local_pdf_state(state: dict[str, str]):
    processed_local_pdf_state.parent.mkdir(parents=True, exist_ok=True)
    processed_local_pdf_state.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _pdf_signature(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_mtime_ns}:{stat.st_size}"


def _build_email_key(doc: dict) -> str:
    message_id = (doc.get("message_id") or "").strip()
    if message_id:
        return message_id
    # Fallback for emails without Message-ID headers.
    raw = f"{doc.get('subject', '')}|{doc.get('body', '')[:2000]}"
    return "fallback:" + hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def _looks_meaningful_email_text(text: str) -> bool:
    if not text:
        return False
    cleaned = re.sub(r"https?://\S+", " ", text)
    alpha_count = sum(ch.isalpha() for ch in cleaned)
    return alpha_count >= 40

def ingest_emails():
    print("Fetch emails...")
    seen_email_ids = _load_processed_email_ids()
    docs = email_fetcher.fetch_new_emails(skip_message_ids=seen_email_ids)
    indexed = 0
    skipped_duplicates = 0

    for doc in docs:
        email_key = _build_email_key(doc)
        if email_key in seen_email_ids:
            skipped_duplicates += 1
            continue

        body_text = (doc.get("body") or "").strip()
        body_chunks = []
        if _looks_meaningful_email_text(body_text):
            body_chunks = chunk_text(body_text, cfg["rag"]["chunk_size"], cfg["rag"]["chunk_overlap"])

        if body_chunks:
            meta = [
                {
                    "text": c,
                    "source": "email",
                    "subject": doc["subject"],
                    "date": doc.get("date", ""),
                    "newsletter_path": doc.get("newsletter_path", ""),
                }
                for c in body_chunks
            ]
            store.add(None, meta)
            indexed += 1

        for path in doc.get("pdf_paths", []):
            parsed = parse_pdf(path, backend="docling")
            pdf_chunks = chunk_parsed_pdf(parsed, cfg["rag"]["chunk_size"], cfg["rag"]["chunk_overlap"])
            if pdf_chunks:
                pdf_meta = [
                    {
                        "text": c.text,
                        "source": "pdf",
                        "filename": Path(path).name,
                        "subject": doc["subject"],
                        "date": doc.get("date", ""),
                        **({"page": c.page} if c.page else {}),
                    }
                    for c in pdf_chunks
                ]
                store.add(None, pdf_meta)
                indexed += 1

        _append_processed_email_id(email_key)
        seen_email_ids.add(email_key)

    print(f"Processed emails: total={len(docs)}, indexed={indexed}, duplicate_skipped={skipped_duplicates}")


def ingest_local_pdfs(pdf_dir: str | None = None):
    if pdf_dir is None:
        pdf_dir = str(resolve_pdf_scan_root(cfg))
    root = Path(pdf_dir)
    if not root.exists():
        print(f"Local PDF directory not found: {pdf_dir}")
        return

    state = _load_local_pdf_state()
    pdf_files = sorted(root.rglob("*.pdf"))
    if not pdf_files:
        print(f"No local PDF files found in {root}")
        return

    added = 0
    updated = 0
    skipped_unchanged = 0
    for pdf_path in pdf_files:
        abs_path = str(pdf_path.resolve())
        try:
            signature = _pdf_signature(pdf_path)
        except OSError as e:
            print(f"[WARN] local pdf stat failed: {pdf_path} - {e}")
            continue

        previous_signature = state.get(abs_path)
        if previous_signature == signature:
            skipped_unchanged += 1
            continue

        try:
            parsed = parse_pdf(abs_path, backend="docling")
            pdf_chunks = chunk_parsed_pdf(parsed, cfg["rag"]["chunk_size"], cfg["rag"]["chunk_overlap"])
            if pdf_chunks:
                if previous_signature is not None:
                    deleted = store.delete_by_metadata({"source": "pdf", "source_path": abs_path})
                    if deleted:
                        print(f"[INFO] local pdf updated, replaced old chunks: {pdf_path.name} (deleted={deleted})")

                docs = [
                    {
                        "text": c.text,
                        "source": "pdf",
                        "filename": pdf_path.name,
                        "source_path": abs_path,
                        **({"page": c.page} if c.page else {}),
                    }
                    for c in pdf_chunks
                ]
                store.add(None, docs)

            _append_processed_pdf_path(abs_path)
            state[abs_path] = signature
            if previous_signature is None:
                added += 1
            else:
                updated += 1
        except Exception as e:
            print(f"[WARN] local pdf parse failed: {pdf_path} - {e}")

    _save_local_pdf_state(state)
    print(
        "local pdf sync done: "
        f"added={added}, updated={updated}, skipped_unchanged={skipped_unchanged}, total={len(pdf_files)}"
    )

def cleanup_old_pdfs(pdf_dir: str = "data/pdfs", retention_days: int = 15):
    """Delete downloaded PDFs older than retention_days.

    Only deletes files already recorded in processed_pdfs.txt (i.e. already
    chunked and embedded into the FAISS index), so retrieval is unaffected —
    the extracted text lives on in data/faiss_index independently of the
    source PDF.
    """
    root = Path(pdf_dir)
    if not root.exists():
        return

    processed = _load_processed_pdf_paths()
    cutoff = time.time() - retention_days * 86400
    deleted = 0
    kept_unprocessed = 0
    for pdf_path in root.rglob("*.pdf"):
        if pdf_path.stat().st_mtime > cutoff:
            continue
        abs_path = str(pdf_path.resolve())
        if abs_path not in processed:
            kept_unprocessed += 1
            continue
        try:
            pdf_path.unlink()
            deleted += 1
        except OSError as e:
            print(f"[WARN] failed to delete {pdf_path}: {e}")

    print(f"pdf cleanup done: deleted={deleted}, kept_unprocessed={kept_unprocessed}")


def ingest_market():
    print("Fetch market data...")
    docs = market_fetcher.fetch()
    for doc in docs:
        chunks = chunk_text(doc["text"], cfg["rag"]["chunk_size"], cfg["rag"]["chunk_overlap"])
        if chunks:
            meta = [{"text": c, "source": "market", "symbol": doc["symbol"], "timestamp": doc["timestamp"]} for c in chunks]
            store.add(None, meta)
    print(f"Processed market items: {len(docs)}")

PDF_RETENTION_DAYS = int(cfg.get("email", {}).get("pdf_retention_days", 15))

SCHEDULER_LOG = Path("data/logs/scheduler.log")


def _log(line: str):
    stamped = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}"
    print(stamped, flush=True)
    try:
        SCHEDULER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with SCHEDULER_LOG.open("a", encoding="utf-8") as f:
            f.write(stamped + "\n")
    except OSError:
        pass


def _logged(job):
    """Run a scheduled job, recording start/end/duration and any exception in
    data/logs/scheduler.log. An exception is logged and swallowed so one failed
    run (e.g. Gmail refusing the login) never stops later runs."""
    def run(*args, **kwargs):
        started = time.monotonic()
        _log(f"[job] {job.__name__} start")
        try:
            job(*args, **kwargs)
        except Exception as e:
            _log(f"[job] {job.__name__} FAILED after {time.monotonic() - started:.0f}s: {type(e).__name__}: {e}")
        else:
            _log(f"[job] {job.__name__} ok in {time.monotonic() - started:.0f}s")
    run.__name__ = job.__name__
    return run


EMAIL_INTERVAL_MIN = int(cfg["email"]["check_interval_minutes"])
PDF_INTERVAL_MIN = int(cfg.get("rag", {}).get("pdf_scan_interval_minutes", 30))
MARKET_INTERVAL_MIN = int(cfg["market"]["fetch_interval_minutes"])

# Interval jobs count from the moment this process starts (not wall-clock times), and
# every job also runs once at startup. coalesce: a backlog of missed runs (laptop
# asleep, long job) collapses into one run; max_instances=1: a job never overlaps itself.
scheduler = BlockingScheduler(job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})
# Fixed wall-clock times (local time) when email.check_times is set, else every N minutes.
EMAIL_CHECK_TIMES = parse_check_times(cfg["email"].get("check_times"))
if EMAIL_CHECK_TIMES:
    for hour, minute in EMAIL_CHECK_TIMES:
        scheduler.add_job(_logged(ingest_emails), "cron", hour=hour, minute=minute,
                          id=f"ingest_emails_{hour:02d}{minute:02d}")
else:
    scheduler.add_job(_logged(ingest_emails), "interval", minutes=EMAIL_INTERVAL_MIN, id="ingest_emails")
scheduler.add_job(_logged(ingest_market), "interval", minutes=MARKET_INTERVAL_MIN, id="ingest_market")
scheduler.add_job(_logged(ingest_local_pdfs), "interval", minutes=PDF_INTERVAL_MIN, id="ingest_local_pdfs")
scheduler.add_job(_logged(cleanup_old_pdfs), "interval", days=1, id="cleanup_old_pdfs",
                  kwargs={"retention_days": PDF_RETENTION_DAYS})

if __name__ == "__main__":
    # Run once at startup
    _logged(ingest_local_pdfs)()
    _logged(ingest_emails)()
    _logged(ingest_market)()
    _logged(cleanup_old_pdfs)(retention_days=PDF_RETENTION_DAYS)
    email_schedule_text = (
        "daily at " + ", ".join(f"{h:02d}:{m:02d}" for h, m in EMAIL_CHECK_TIMES)
        if EMAIL_CHECK_TIMES else f"every {EMAIL_INTERVAL_MIN} min"
    )
    _log(
        f"Scheduler started: emails {email_schedule_text}, local PDFs every {PDF_INTERVAL_MIN} min, "
        f"market every {MARKET_INTERVAL_MIN} min, PDF cleanup daily"
    )
    scheduler.start()
