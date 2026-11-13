#!/usr/bin/env python
# ============================================================
#  eval/run_eval.py — CLI entrypoint for the agentic RAG eval harness
#
#  Usage:
#    python eval/run_eval.py
#    python eval/run_eval.py --dataset eval/datasets/core.yaml
#    python eval/run_eval.py --filter category=rule_compliance
#    python eval/run_eval.py --case-id isin_gate_blocks_bare_name
#
#  Runs live against the configured MiniMax-backed agent and your local
#  FAISS index/watchlist (config.yaml), so each run costs a small number of
#  real API calls. Exits non-zero if any case fails (usable in CI later).
# ============================================================

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval.adapters.single_agent import SingleAgentAdapter
from eval.report import load_dataset, run_cases, print_summary, write_results


def _parse_filter(expr: str | None) -> tuple[str, str] | None:
    if not expr:
        return None
    key, _, value = expr.partition("=")
    if not _:
        raise SystemExit(f"--filter must look like key=value, got: {expr!r}")
    return key.strip(), value.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the agentic RAG eval harness.")
    parser.add_argument("--dataset", default=str(REPO_ROOT / "eval" / "datasets" / "core.yaml"))
    parser.add_argument("--filter", default=None, help="e.g. category=rule_compliance")
    parser.add_argument("--case-id", default=None, help="Run only the case with this id")
    parser.add_argument("--no-write", action="store_true", help="Skip writing eval/results/*.jsonl")
    args = parser.parse_args()

    cases = load_dataset(args.dataset)

    if args.case_id:
        cases = [c for c in cases if c["id"] == args.case_id]
        if not cases:
            raise SystemExit(f"No case with id={args.case_id!r} in {args.dataset}")

    filt = _parse_filter(args.filter)
    if filt:
        key, value = filt
        cases = [c for c in cases if str(c.get(key)) == value]
        if not cases:
            raise SystemExit(f"No cases match filter {args.filter!r} in {args.dataset}")

    print(f"Running {len(cases)} case(s) from {args.dataset}\n")

    adapter = SingleAgentAdapter()
    rows = run_cases(adapter, cases)

    print_summary(rows)

    if not args.no_write:
        out_path = write_results(rows)
        print(f"\nWrote results to {out_path.relative_to(REPO_ROOT)}")

    return 0 if all(r["passed"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
