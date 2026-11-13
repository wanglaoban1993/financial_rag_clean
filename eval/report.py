# ============================================================
#  eval/report.py — run eval cases through an adapter, print a summary,
#  and persist the full run as JSONL for later diffing.
# ============================================================

from __future__ import annotations

import json
import time
from pathlib import Path

import yaml

from eval.evaluators import tool_selection, rule_compliance, retrieval

EVALUATOR_MODULES = [tool_selection, rule_compliance, retrieval]

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_DIR = REPO_ROOT / "eval" / "results"


def load_dataset(path: str | Path) -> list[dict]:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or []


def run_cases(adapter, cases: list[dict]) -> list[dict]:
    """Run each case through the adapter + all evaluators; return per-case result dicts."""
    rows = []
    for case in cases:
        trace = adapter.run(case["query"], case.get("history"))

        checks = []
        for module in EVALUATOR_MODULES:
            checks.extend(module.evaluate(case, trace))
        passed = all(c.passed for c in checks) if checks else True

        rows.append({
            "id": case["id"],
            "category": case.get("category", "uncategorized"),
            "passed": passed,
            "checks": [{"check": c.check, "passed": c.passed, "detail": c.detail} for c in checks],
            "query": case["query"],
            "final_answer": trace.final_answer,
            "tool_calls": [{"tool": tc.tool, "args": tc.args} for tc in trace.tool_calls],
            "error": trace.error,
        })
    return rows


def print_summary(rows: list[dict]) -> None:
    width = max((len(r["id"]) for r in rows), default=2)
    for r in rows:
        status = "PASS" if r["passed"] else "FAIL"
        print(f"[{status}] {r['id']:<{width}}  ({r['category']})")
        if not r["passed"]:
            for c in r["checks"]:
                if not c["passed"]:
                    print(f"         - {c['check']}: {c['detail']}")
            if r["error"]:
                print(f"         - error: {r['error']}")

    print()
    by_category: dict[str, list[bool]] = {}
    for r in rows:
        by_category.setdefault(r["category"], []).append(r["passed"])
    for cat, results in sorted(by_category.items()):
        n_pass = sum(results)
        print(f"{cat}: {n_pass}/{len(results)} passed")

    total_pass = sum(r["passed"] for r in rows)
    print(f"\nTOTAL: {total_pass}/{len(rows)} passed")


def write_results(rows: list[dict]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_path = RESULTS_DIR / f"{timestamp}.jsonl"
    with out_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return out_path
