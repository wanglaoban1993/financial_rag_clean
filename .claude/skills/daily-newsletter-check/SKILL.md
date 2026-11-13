---
name: daily-newsletter-check
description: Daily brief from the indexed finance newsletters and PDFs, with every checkable claim (target price, resistance/support level, trend call) verified against a live quote. Use when the user asks for the daily newsletter summary, "what do today's reports say and does the market agree", or a Newsletter/Kurs-Abgleich. Needs the agent-api container running on port 8100.
---

# Daily newsletter summary + market check

Turns the newsletters and PDFs already in the FAISS index into one report in which every
claim is either backed by a source and a live quote, or marked as open. It reuses the
project's agent API (`server_app/agent_api.py`, `POST /chat/stream`), so retrieval,
the ISIN/name resolution and the grounding check are the same as in the dashboard.

## When to use

- "Mach die tägliche Newsletter-Zusammenfassung", "was sagen die heutigen Reports?"
- "Stimmen die Kursziele/Marken aus den Newslettern mit den aktuellen Kursen überein?"

Not for a single price lookup or a one-off question about one document: use the dashboard
chat or `query.py --agent` for that.

## Steps

1. Check the service is up: `curl -s http://127.0.0.1:8100/health` must show `"agent_ready": true`.
   If not: `docker compose up -d agent-api` (from the project root), wait for healthy.
2. Run the script from the project root:

   ```sh
   python .claude/skills/daily-newsletter-check/scripts/daily_brief.py --days 1
   ```

   Options: `--days N` (look-back window, default 1), `--date YYYY-MM-DD` (end of the window,
   default today), `--max-documents N` (default 5), `--max-instruments N` (default 6), `--url` (default `$AGENT_API_URL` or
   `http://127.0.0.1:8100`), `--out-dir` (default `reports/`).
   The script uses only the standard library, any Python 3.9+ works.
3. The script writes `reports/daily_brief_<date>.md` and prints it. Read the report and
   check it against the rules below before presenting it.
4. Tell the user the path of the report, the verdict counts, and anything flagged.

## What the script does

1. **List**: asks the agent to call `list_recent_documents` and print one line per document.
   The script itself applies the date window and takes the newest `--max-documents` (default 5),
   so the model cannot widen or shift it.
2. **Summarise, one document per call**: the agent reads the newsletter (`read_email`) or PDF
   (`read_file` / `search_documents`) and gives 2-4 statements the text supports, ending with
   a `## Zu prüfen` list of `- <Name/ISIN>: <checkable claim>`. One call for all documents
   exhausts the agent's tool budget (8 steps) and comes back `[Incomplete]`; per document it fits.
3. **Check**: the claims are deduplicated; for each one (at most `--max-instruments`) the agent
   fetches the live quote and ends with a line `Urteil: bestätigt|widerlegt|offen` plus the
   distance to the stated level in %. Verdict rules, fixed in the prompt: resistance is
   `bestätigt` while the price is below it and `widerlegt` once above; support the other way
   round; a target price is `bestätigt` once reached, else `offen`; no quote with timestamp
   means `offen`. The script reads only that `Urteil:` line; without one the verdict is `offen`.
4. **Report**: per-document summaries, a table of claim vs. verdict with the quote detail,
   the sources of every step, and the grounding warnings (numbers/dates in an answer that appear
   in no retrieved evidence).

## Rules for the report

- A quote counts only with its timestamp. A price answer without a date is not a check;
  re-run that item or mark it `offen`.
- `grounding_warnings` are heuristic ("worth a second look", not proof of a hallucination).
  Pass them on, do not silently drop them and do not call them errors.
- If the window contains no documents, say so. Do not widen the window or fill the gap
  from general knowledge.
- Never present a newsletter's opinion as a fact: the report says "Newsletter X sieht ...".
- Runtime is about 1 minute per agent call: the defaults (5 documents, 6 claims) take roughly
  15 minutes. Use `--max-documents 2 --max-instruments 3` for a quick run.
- Grounding warnings on a check step that are computed values (distance in %, differences)
  are expected: they are derived, not retrieved. A warning on a quote or a level is not.
- Quotes can be delayed or come from a fallback source (rows marked `(Yahoo)`); keep that marker.

## Failure modes

| Symptom | Meaning / action |
|---|---|
| exit code 2, "agent API not reachable" | container not running; start `agent-api` |
| exit code 3, `[Incomplete]` in a step | the agent ran out of tool budget; the report keeps what was read and flags the step |
| empty `## Zu prüfen` | no document produced a checkable claim; the report says so and skips the check step |
| "Keine Dokumente im Zeitraum" | nothing in the index inside the window (PDFs are dated by the date in their filename or their file-added date) |
| "message limit for today" | `DEMO_DAILY_MESSAGE_LIMIT` is set on the agent container |
