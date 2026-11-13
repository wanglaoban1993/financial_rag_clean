#!/usr/bin/env python3
# ============================================================
#  daily_brief.py -- newsletter/PDF summary + live-quote check.
#
#  Client for the project's agent API (POST /chat/stream). Standard library
#  only, so it runs outside the conda env. See ../SKILL.md for the workflow.
#
#  Exit codes: 0 ok, 2 API unreachable, 3 at least one step was incomplete or
#  failed (the report is still written).
# ============================================================

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8100"
PARTIAL_PREFIX = "[Incomplete]"
CHECK_HEADING = re.compile(r"^#{1,6}\s*Zu pr(?:ü|ue)fen\s*$", re.IGNORECASE | re.MULTILINE)
CHECK_ITEM = re.compile(r"^\s*[-*]\s+(?P<name>[^:]{2,80}?)\s*:\s*(?P<claim>.+?)\s*$")
# Only an explicit "Urteil: <word>" line counts; the words also occur in prose
# ("weder bestätigt noch widerlegt"), which must not be read as a verdict.
VERDICT = re.compile(r"urteil\W{0,6}(best(?:ä|ae)tigt|widerlegt|offen)\b", re.IGNORECASE)

LIST_PROMPT = (
    "Rufe list_recent_documents mit source=all und limit=30 auf. Gib danach alle Dokumente aus, "
    "eine Zeile pro Dokument im Format: - <Titel oder Dateiname> | <Datum YYYY-MM-DD>. "
    "Kein weiterer Text."
)

SUMMARY_PROMPT = (
    "Fasse dieses Dokument zusammen: {title} (Datum {day}). Lies es mit read_email (Newsletter) "
    "bzw. read_file oder search_documents (PDF). Gib die 2-4 wichtigsten Aussagen wieder "
    "(Aktie oder Index, Einschätzung, Kursziel oder Chartmarke). Nur Aussagen, die der gelesene "
    "Text trägt. Beende die Antwort mit einem Abschnitt '## Zu prüfen' mit höchstens {limit} Zeilen "
    "im Format '- <Name oder ISIN, ohne Kurs und Zusätze>: <Widerstand|Unterstützung|Kursziel> <Zahl> <Währung>', "
    "z.B. '- Siemens AG: Widerstand 24.500 EUR'. Nur Marken, die das Dokument selbst nennt; "
    "keine Fragen, keine eigenen Prognosen."
)

CHECK_PROMPT = (
    "Prüfe diese Aussage aus einem Newsletter gegen den aktuellen Kurs. "
    "Instrument: {name}. Aussage: {claim}. "
    "Hole den aktuellen Kurs mit get_market_quote und nenne ihn mit Datum und Uhrzeit. "
    "Gib den Abstand zur genannten Marke in Prozent an. Regeln: Widerstand ist bestätigt, wenn der Kurs "
    "darunter notiert, widerlegt, wenn er darüber notiert; Unterstützung ist bestätigt, wenn der Kurs "
    "darüber notiert, widerlegt, wenn er darunter notiert; ein Kursziel ist bestätigt, wenn der Kurs "
    "es erreicht oder überschritten hat, sonst offen. Ohne Kurs mit Zeitstempel ist das Urteil offen. "
    "Schließe mit genau einer letzten Zeile: 'Urteil: bestätigt', 'Urteil: widerlegt' oder 'Urteil: offen'."
)

DOC_LINE = re.compile(r"^\s*[-*]\s+(?P<title>.+?)\s*\|\s*(?P<day>\d{4}-\d{2}-\d{2})\s*$")


class ApiError(Exception):
    pass


def _request(url: str, payload: dict | None, timeout: float) -> urllib.request.addinfourl:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    return urllib.request.urlopen(req, timeout=timeout)


def check_health(base_url: str, timeout: float = 10.0) -> None:
    try:
        with _request(f"{base_url}/health", None, timeout) as resp:
            health = json.load(resp)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiError(f"agent API not reachable at {base_url}: {exc}") from exc
    if not health.get("agent_ready"):
        raise ApiError(f"agent API at {base_url} is up but the agent is not ready yet")


def ask(base_url: str, message: str, timeout: float, history: list[dict] | None = None) -> dict:
    """One /chat/stream call. Returns {"answer", "sources", "grounding_warnings"};
    raises ApiError on an "error" event or a stream without an answer."""
    payload = {"message": message, "history": history or []}
    try:
        with _request(f"{base_url}/chat/stream", payload, timeout) as resp:
            final = None
            for raw in resp:
                line = raw.decode("utf-8").strip()
                if not line:
                    continue
                event = json.loads(line)
                if event.get("type") == "error":
                    raise ApiError(event.get("message", "unknown agent error"))
                if event.get("type") == "answer":
                    final = event
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ApiError(f"request failed: {exc}") from exc
    if final is None:
        raise ApiError("stream ended without an answer")
    return {
        "answer": final.get("answer", ""),
        "sources": final.get("sources") or [],
        "grounding_warnings": final.get("grounding_warnings") or [],
    }


def parse_check_items(summary: str, limit: int) -> list[dict]:
    """Bullets under the '## Zu prüfen' heading as [{"name", "claim"}]."""
    match = CHECK_HEADING.search(summary)
    if not match:
        return []
    items = []
    for line in summary[match.end():].splitlines():
        if line.lstrip().startswith("#"):
            break
        item = CHECK_ITEM.match(line)
        if item:
            items.append({"name": item["name"].strip("*_ "), "claim": item["claim"].strip("*_ ")})
        if len(items) >= limit:
            break
    return items


def split_summary(summary: str) -> str:
    """The summary without its trailing '## Zu prüfen' list (shown as a table instead)."""
    match = CHECK_HEADING.search(summary)
    return (summary[:match.start()] if match else summary).rstrip()


def verdict_of(answer: str) -> str:
    """The word of the last explicit 'Urteil: ...' line; 'offen' if there is none."""
    found = VERDICT.findall(answer)
    if not found:
        return "offen"
    word = found[-1].lower().replace("ae", "ä")
    return word


def parse_documents(listing: str, start: date, end: date, limit: int) -> list[dict]:
    """'- <title> | <YYYY-MM-DD>' lines inside [start, end], newest first, at most `limit`.
    The window is applied here, not by the model, so it cannot drift."""
    docs = []
    for line in listing.splitlines():
        m = DOC_LINE.match(line)
        if not m:
            continue
        try:
            day = date.fromisoformat(m["day"])
        except ValueError:
            continue
        if start <= day <= end:
            docs.append({"title": m["title"].strip("*_ "), "day": day.isoformat()})
    docs.sort(key=lambda d: d["day"], reverse=True)
    return docs[:limit]


def is_partial(answer: str) -> bool:
    return answer.lstrip().startswith(PARTIAL_PREFIX)


def _source_line(src: dict) -> str:
    bits = [src.get("kind", ""), src.get("title", ""), src.get("detail", "")]
    return " | ".join(b for b in bits if b)


def build_report(start: date, end: date, summaries: list[dict], checks: list[dict]) -> str:
    lines = [f"# Daily brief {end.isoformat()}", "",
             f"Zeitraum der Dokumente: {start.isoformat()} bis {end.isoformat()}", "",
             "## Zusammenfassung", ""]
    if not summaries:
        lines += [f"Keine Dokumente im Zeitraum {start.isoformat()} bis {end.isoformat()} im Index.", ""]
    for doc in summaries:
        lines += [f"### {doc['title']} ({doc['day']})", "",
                  split_summary(doc["answer"]) or "(leer)", ""]

    if checks:
        lines += ["## Kursabgleich", "", "| Instrument | Aussage | Urteil |", "|---|---|---|"]
        for c in checks:
            cell = c["verdict"] if not c["partial"] else f"{c['verdict']} (unvollständig)"
            lines.append(f"| {c['name']} | {c['claim']} | {cell} |")
        lines.append("")
        for c in checks:
            lines += [f"### {c['name']}", "", c["answer"].strip() or "(keine Antwort)", ""]
    else:
        lines += ["## Kursabgleich", "", "Keine prüfbaren Aussagen gefunden; kein Kursabgleich durchgeführt.", ""]

    sources: list[str] = []
    for step in summaries + checks:
        for src in step.get("sources", []):
            line = _source_line(src)
            if line and line not in sources:
                sources.append(line)
    lines += ["## Quellen", ""] + ([f"- {s}" for s in sources] or ["- (keine)"]) + [""]

    warnings = [(label, w) for label, step in
                [(d["title"], d) for d in summaries] + [(c["name"], c) for c in checks]
                for w in step.get("grounding_warnings", [])]
    lines += ["## Grounding-Hinweise", "",
              "Heuristik: Zahlen/Daten in der Antwort, die in keiner abgerufenen Quelle stehen. "
              "Ein Hinweis ist kein Beweis für eine Halluzination.", ""]
    lines += [f"- {label}: {w}" for label, w in warnings] or ["- keine"]
    return "\n".join(lines).rstrip() + "\n"


def run(base_url: str, end: date, days: int, max_documents: int, max_instruments: int,
        timeout: float) -> tuple[str, bool]:
    """Returns (report markdown, ok). ok is False if any step was partial or failed."""
    start = end - timedelta(days=max(days, 1) - 1)
    ok = True

    listing = ask(base_url, LIST_PROMPT, timeout)
    docs = parse_documents(listing["answer"], start, end, max_documents)

    summaries: list[dict] = []
    for doc in docs:
        try:
            result = ask(base_url, SUMMARY_PROMPT.format(limit=3, **doc), timeout)
        except ApiError as exc:
            result = {"answer": f"[Fehler] {exc}", "sources": [], "grounding_warnings": []}
        if is_partial(result["answer"]) or result["answer"].startswith("[Fehler]"):
            ok = False
        summaries.append({**doc, **result})

    items: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for doc in summaries:
        for item in parse_check_items(doc["answer"], max_instruments):
            key = (item["name"].lower(), item["claim"].lower())
            if key not in seen and len(items) < max_instruments:
                seen.add(key)
                items.append(item)

    checks: list[dict] = []
    for item in items:
        try:
            result = ask(base_url, CHECK_PROMPT.format(**item), timeout)
        except ApiError as exc:
            result = {"answer": f"[Fehler] {exc}", "sources": [], "grounding_warnings": []}
        partial = is_partial(result["answer"]) or result["answer"].startswith("[Fehler]")
        if partial:
            ok = False
        # Numbers taken from the claim we put in the prompt are not evidence gaps.
        result["grounding_warnings"] = [
            w for w in result["grounding_warnings"] if w not in item["claim"]]
        checks.append({**item, **result, "partial": partial,
                       "verdict": "offen" if partial else verdict_of(result["answer"])})

    return build_report(start, end, summaries, checks), ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Newsletter summary + live-quote check")
    parser.add_argument("--days", type=int, default=1, help="look-back window in days (default 1)")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(),
                        help="last day of the window, YYYY-MM-DD (default today)")
    parser.add_argument("--max-documents", type=int, default=5, help="documents to summarise (newest first)")
    parser.add_argument("--max-instruments", type=int, default=6)
    parser.add_argument("--url", default=os.getenv("AGENT_API_URL", DEFAULT_URL))
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--timeout", type=float, default=300.0, help="seconds per agent call")
    args = parser.parse_args(argv)

    base_url = args.url.rstrip("/")
    try:
        check_health(base_url)
        report, ok = run(base_url, args.date, args.days, args.max_documents,
                            args.max_instruments, args.timeout)
    except ApiError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"daily_brief_{args.date.isoformat()}.md"
    out.write_text(report, encoding="utf-8")
    print(report)
    print(f"report written to {out}", file=sys.stderr)
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
