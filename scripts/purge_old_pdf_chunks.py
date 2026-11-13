"""Remove pre-page-metadata PDF chunks once a file has been re-indexed with pages.

For every PDF currently in the scan folder, the old chunks (source == "pdf" and
no `page` field) are deleted -- but only if the same filename already has new
chunks that carry a `page`, so a failed re-parse never leaves a file empty.
PDFs that are only in the index (no longer on disk) are never touched.

Dry run by default. Stop financial-rag-main first when using --apply: it holds
the index in memory and would overwrite the change on its next save.

    python scripts/purge_old_pdf_chunks.py            # report only
    python scripts/purge_old_pdf_chunks.py --apply    # back up index, then delete
"""

import argparse
import shutil
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langchain_community.embeddings import FakeEmbeddings  # deletes never embed

from config_loader import load_config
from rag.pdf_watch import resolve_pdf_scan_root
from rag.vector_store import FAISSVectorStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually delete (default: dry run)")
    parser.add_argument("--index-dir", default="data/faiss_index")
    parser.add_argument("--pdf-dir", default=None, help="default: rag.pdf_scan_root from config")
    args = parser.parse_args()

    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else resolve_pdf_scan_root(load_config())
    on_disk = {p.name for p in pdf_dir.rglob("*.pdf")}
    print(f"{len(on_disk)} PDFs in {pdf_dir}")

    embedder = SimpleNamespace(lc_embeddings=FakeEmbeddings(size=1024), dim=1024)
    store = FAISSVectorStore(embedder=embedder, index_dir=args.index_dir)

    old_ids: dict[str, list[str]] = {}
    new_counts: Counter = Counter()
    for _, doc_id in store.vs.index_to_docstore_id.items():
        doc = store.vs.docstore.search(doc_id)
        meta = getattr(doc, "metadata", {}) or {}
        name = meta.get("filename")
        if meta.get("source") != "pdf" or name not in on_disk:
            continue
        if "page" in meta:
            new_counts[name] += 1
        else:
            old_ids.setdefault(name, []).append(doc_id)

    to_delete: list[str] = []
    print(f"\n{'old':>6} {'new':>6}  file")
    for name in sorted(on_disk):
        old, new = len(old_ids.get(name, [])), new_counts[name]
        if old and new:
            verdict, to_delete = "purge", to_delete + old_ids[name]
        elif old:
            verdict = "SKIP (no re-indexed chunks with page yet)"
        else:
            verdict = "clean" if new else "not indexed"
        print(f"{old:6d} {new:6d}  {name}  -> {verdict}")

    print(f"\n{len(to_delete)} old chunks to delete, {sum(new_counts.values())} page-tagged chunks kept")
    if not args.apply:
        print("Dry run -- nothing changed. Re-run with --apply (service stopped) to delete.")
        return
    if not to_delete:
        return

    backup = Path(f"{args.index_dir}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copytree(args.index_dir, backup)
    print(f"Backed up index to {backup}")
    store.vs.delete(ids=to_delete)
    store._save()
    print(f"Deleted {len(to_delete)} chunks; index now has {store.vs.index.ntotal} vectors.")


if __name__ == "__main__":
    main()
