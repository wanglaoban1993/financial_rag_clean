# Agent design: problems found and the solutions used

Written for: maintainers of this repo. It records what went wrong in the agentic RAG chat
(dashboard assistant), the cause that was found for each problem, what was changed, and what
is still open. Everything below was observed in real conversations and checked against the
index, the code or the data provider, not assumed. Dates are October 2026.

Short version of the architecture this refers to: `rag/orchestrator.py` routes a question to
`DocumentAgent` (PDFs, emails, watchlists) and/or `MarketAgent` (EODHD / Finnhub market data);
each is a `ToolCallingAgent` (`rag/tool_calling_agent.py`), a ReAct-style loop that parses tool
calls out of the model's text. See the README for the full flow.

## 1. Reading documents

### 1.1 "Give me the full content of this email" could not be answered

| | |
|---|---|
| **Symptom** | The assistant searched repeatedly, used up its tool calls, and then said emails are "not accessible as files". |
| **Cause** | No tool returned a whole email. `search_documents` returns 800-character passages and `read_file` only knew PDF/CSV file names. The email existed in the index as 139 chunks (about 53,600 characters). |
| **Solution** | `read_email(subject)` rebuilds the email from its chunks (`FAISSVectorStore.get_chunks_by_subject`: case-insensitive, ignores a leading `Fwd:`/`Re:`, newest date wins, ambiguous partial matches return nothing). Chunk overlap and repeated headings are removed by the existing `_stitch_chunks`. |
| **Files** | `rag/agent_tools.py`, `rag/vector_store.py`, `rag/document_agent.py` |

### 1.2 "Read today's PDF" could not find the file

| | |
|---|---|
| **Symptom** | The assistant did not know which file "today's PDF" was, and claimed PDFs were not readable. |
| **Cause** | PDF chunks carry no publication date. The only date in the registry is when the file was added. The file name is the only place with the real date (`..._20261008.pdf`, or `..._03082026.pdf`). A further trap: in `data/newsletters/` 15 email files are prefixed `20261008`, but only one email is actually dated 2026-10-08. The prefix is the *ingestion* date, because older mails were back-filled that day. |
| **Solution** | `doc_inventory.date_from_filename` parses `YYYYMMDD` and `DDMMYYYY`, rejects impossible and future dates (a typo such as `20280821` is not reported as news), and labels the date's origin (`date_basis`). `list_recent_documents` and `search_documents` use it. The prompt tells the agent to list PDFs first, then `read_file` the exact file name. |
| **Files** | `rag/doc_inventory.py`, `rag/agent_tools.py`, `rag/document_agent.py` |
| **Note** | In the agent container only `data/` is mounted, not the host's `~/Projects/pdfs`, so `read_file` for these PDFs always uses the index fallback (text rebuilt from chunks). That works as long as the exact file name is given. |

### 1.3 Long documents were truncated, and the model misreported where

| | |
|---|---|
| **Symptom** | A 53,600-character email came back cut at 20,000 characters; the answer said it was "truncated at ~54,000 characters". A 17,000-character PDF was also described as truncated. |
| **Cause** | The tool cut the text silently and the model guessed at the numbers. |
| **Solution** | `read_file` and `read_email` page their output: `offset` parameter, and every result states `chars_returned` ("0-20000 of 53628"), `total_chars`, and, when there is more, `next_offset` plus a `PARTIAL` instruction. The working rules require the agent to say how much it read and never to guess where or why text was cut. |
| **Files** | `rag/agent_tools.py` (`_page`), `rag/tool_calling_agent.py` (`COMMON_RULES`) |

### 1.4 Questions about a day's documents were answered by similarity alone

| | |
|---|---|
| **Symptom** | "What does today's newsletter say about the DAX?" (and a query almost identical to today's subject line) returned emails of earlier days: in a check on the live index the top three were dated 09-23, 10-07 and 10-01, not 10-08. When no document existed for the day, the assistant still produced an answer, or invented an email. |
| **Cause** | Retrieval is BM25 + FAISS + reranker over all chunks; metadata (the email `date`, the date in a PDF file name) was stored but never used to narrow a search. The daily newsletters share most of their text, so near-identical days compete. |
| **Solution** | Date-aware search (`rag/date_filter.py`): the range is read from the user's *original* question, only when it names a document and a date, and applied to both retrievers **before** ranking (a filter after the top-k would find nothing: the matching day is a tiny part of the index). An empty day returns `no_documents_in_range` plus the newest available date, and the agent says so. Details are in the README section "Date-aware document search". |
| **Found while testing** | (a) FAISS filters only the `fetch_k` nearest neighbours, so `fetch_k` must be the whole index. (b) A tool-description variable named `label` was overwritten inside a loop, so the "only documents dated ..." note printed an email subject. (c) The model sent `search_documents` with dates but no `query`, got a validation error, resent it unchanged and ended in `[Incomplete]`; a missing query now falls back to the user's question. (d) The relevance grader treated "no document is dated X" as a bad retrieval and would have pushed the model to search again without the date; that result now skips grading. |
| **Limit** | Documents have a date, not a time. "Today" is the server date. Other relative ranges ("last week") and filters by source or file name are not implemented. |

## 2. Fabricated and unreliable answers

### 2.1 Answers contained content that is not in the documents

| | |
|---|---|
| **Symptom** | Asked to read the HL Report PDF, the assistant produced a full market overview (index levels, ECB remarks, payrolls, RSI/MACD, an event calendar). The indexed text of that PDF (about 17,000 characters, 47 chunks) contains none of it (0 matches for `Sintra`, `Lagarde`, `RSI`, `MACD`, `FOMC`, `Nasdaq`, `23.032`). Another time it "found" an email called "Portfolio-Upload – Bestandskunden", including a holdings table; the index has no such email. |
| **Cause** | The model filled gaps from memory, and nothing in the system could tell it that a document did not exist. A weaker, faster model (MiniMax-M2.7-highspeed) makes this more likely. |
| **Solution (done)** | Working rule 4 in `COMMON_RULES`: report only what the functions returned; say "the document does not contain this" instead of filling gaps; state how much was read. The grounding check (`rag/grounding.py`) flags numbers absent from the tool output. |
| **Still open** | Both measures are advisory. The warning is appended *after* the answer is shown, and a prompt rule cannot guarantee obedience. The robust fix is structural (section 6). |

### 2.2 The grounding check produced false alarms

| | |
|---|---|
| **Symptom** | Correct DAX levels from a PDF (25,164, 25,438, ...) were reported as "not found in the retrieved sources". |
| **Cause** | PDF extraction leaves a space or line break after the thousands separator (`25. 164`, `26.` newline `000`), so the check saw two numbers. |
| **Solution** | `check_numeric_grounding` also searches a copy of the evidence with those separators joined (`_with_repaired_numbers`). Both copies are kept, so repairing can only add matches. Verified on the real PDF. Genuinely wrong numbers are still flagged. |
| **Limit** | It compares literal numbers. A correct conversion ("7 von 10" written as 70%) or an inferred year is still flagged. Treat the list as "worth a look", not proof of an error. |

### 2.3 Earlier wrong answers were fed back to the model

| | |
|---|---|
| **Symptom** | After one bad answer, later answers repeated its claims (for example "I cannot read PDFs"). |
| **Cause** | `Orchestrator.query` passed the last 6 messages to the agents *and* counted them as grounding evidence, so an answer with invented figures vouched for itself. |
| **Solution** | `rag/history.py`: an earlier assistant answer that is an error, carries the grounding footer, or was remembered by this process as flagged is replaced by a short placeholder before it is used as context or evidence. The user's question is kept. `UntrustedAnswers` is a bounded memory (200 answers) matched on the first 300 normalised characters, so a client that appends a source list still matches. A system note tells the model that earlier messages are context, not sources. |
| **Limit** | A false-positive flag also hides a good answer from follow-ups (the user just asks again). The memory is in-process only. |

### 2.4 Tool data was overridden by the model's background knowledge

| | |
|---|---|
| **Symptom** | `SPCX` returned a price and an ISIN, yet the assistant added "SpaceX is a private company and not publicly listed". |
| **Cause** | The tools gave a price but no statement of what the instrument is; the model supplied its own belief. |
| **Solution** | Working rule 3 (function results beat background knowledge) and `get_instrument_profile`, which lists the instrument's exchange listings (EODHD `/search`, available on every plan). Its result carries a `summary` and a `note`: an instrument with listings is publicly traded and must not be called private or pre-IPO. The MarketAgent prompt repeats this. |
| **Limit** | Still prompt-based. If the model keeps adding the caveat, render this answer from the tool data in code instead of letting the model rewrite it. |

### 2.5 A tool description claimed a data property that was not true

| | |
|---|---|
| **Symptom** | `get_watchlist` told the model that repeated instruments (same ISIN) were "already collapsed" and that `duplicates_removed` reported the count. The data returned the rows as stored, with repeats, so the model was assured there were none. |
| **Cause** | The collapsing code (`server_app/db.py::dedupe_and_rank`) was lost in the overwrite described in section 5, but the description lived in `rag/agent_tools.py` and had been committed. Separately, it turned out the collapsing was not wanted: the same ISIN legitimately appears in several watchlists, and repeated rows inside `top_liste_latest` are normal for this user. |
| **Solution** | The description and the DocumentAgent prompt now say the opposite: rows come back exactly as stored, a repeated instrument is by design, report each row as listed and never treat a repeat as an error or search for a replacement entry (the earlier failure mode was a follow-up run inventing a "missing third entry"). A test pins the wording. |
| **Follow-up finding** | The repeated rows in `top_liste_latest` are one per *index* (the list holds the top/flop stocks of 13 indices, so a stock in two indices has two rows). They looked identical to the model because the distinguishing columns, `Index` and `Rank`, exist only in the `raw` json column and the queries did not select it. `get_watchlist_rows` / `search_watchlist_rows` now return them as `index` and `rank_in_index`, the prompt asks for each row to be reported with its index, and citations of the same stock under two indices stay two entries. |
| **Rule** | A tool description is part of the behaviour: it must be checked against what the code returns. |

## 3. Tool budget and failure behaviour

| | |
|---|---|
| **Symptom** | When the tool-call limit (4) ran out, the agent was told to "write your final answer now" and filled the gaps; the extra call over a very long context also timed out. |
| **Solution** | Per-agent budgets: `ToolCallingAgent.MAX_TOOL_HOPS = 4` (DocumentAgent), `MarketAgent.MAX_TOOL_HOPS = 6` (five tools), and 8 (`MAX_READ_HOPS`) once a read tool has been used. Running out no longer forces an answer: the reply starts with `[Incomplete]`, lists the steps taken with how much of each document was read ("the rest was NOT read"), and gives a summary limited to what was read. If that summary call fails, raw excerpts of the results are shown instead. The critic is skipped for such replies, because asking the same specialist again only spends another budget (but not for route `both` before the second specialist has run). |
| **Files** | `rag/tool_calling_agent.py` (`_incomplete_answer`), `rag/orchestrator.py` (`_critic_skip_reason`), `rag/market_agent.py` |

Error text shown to the user is now one line (`[DocumentAgent error] ... timed out ...`). The
traceback goes to the server log. The `[<name> error]` prefix is kept because the Orchestrator
keys on it.

## 4. Market data: what EODHD offers, and how it was integrated

**Decision: wrap EODHD's REST endpoints as tools, do not connect the EODHD MCP server.**
The official MCP server exposes 72 read-only tools. This project is not an MCP client; its
agents are a hand-written loop that parses tool calls from the model's text, so adding dozens of
tool descriptions would crowd the prompt and make tool choice less reliable. Each wrapped
endpoint is a small `@tool` that can be tested with fakes and limited to what the plan includes.

Endpoints checked read-only with the project's key on 2026-10-09 (plan name could not be
confirmed from public pages, so these are measured results):

| Endpoint | Result | Wrapped as |
|---|---|---|
| `search` (name / ticker / ISIN) | works | `get_instrument_profile`, symbol resolution |
| `eod` (daily history) | works | `get_price_history` |
| `news` | works | `get_company_news` |
| `sentiments` | works | `get_news_sentiment` |
| `real-time`, `intraday` | work | (`real-time` already used for quotes) |
| `technical` | HTTP 200 but empty list | not wrapped; parameters need checking |
| `fundamentals`, `calendar/earnings`, `calendar/dividends`, `calendar/splits`, `calendar/ipos`, `macro-indicator` | HTTP 403 | not available |

Design points:

- `MarketFetcher._eodhd_get` is the single entry point. **Every error is re-raised without the
  request URL**, because `requests` exceptions embed the full URL including `api_token`. HTTP 403
  becomes "not included in the current subscription plan" so the agent can say so.
- `resolve_eodhd_symbol` accepts a ticker, ISIN, `SAP.DE`-style symbol or a name. For a name
  that has no primary listing among the matches it raises `AmbiguousInstrumentError` instead of
  guessing. Real example: the bare name "SpaceX" only finds depositary receipts
  (`SPACEX01.BK`, ...), and the first one used to be picked. The tools then fall back to the
  user's own watchlist, which knows the ISIN behind that name.
- News text is third-party content. The tool result tells the model to treat it as data, to
  ignore instructions inside it, and not to say an article *caused* a move unless the article does.
- `get_price_history` computes each day's change from the closes in code; the model quotes
  numbers instead of calculating them.
- The router prompt and keyword list were extended so news, sentiment, price history and
  "what is this ISIN" questions go to the MarketAgent.

### 4.1 A failure in the identifier hand-off (found while testing the above)

For a market question with only a company name, `Orchestrator._node_resolve_identifier` asks the
DocumentAgent for an ISIN and appends it to the MarketAgent's question. The reply was accepted
unchecked, so an `[Incomplete] ...` notice or an error message ended up in the question as
"(resolved identifier: [Incomplete] I could not find anything ...)", and the MarketAgent repeated
it ("document search incomplete"). `_clean_identifier` now accepts only a real ISIN/WKN/ticker
(an ISIN inside a sentence is extracted; `NOT_FOUND`, `[Incomplete]`, errors, long sentences are
dropped).

## 5. Process lesson: pushing deploys, and deploying overwrites

`scripts/deploy_dashboard.sh` runs from cron (every hour when the problem below happened; now once a day at 05:00: `0 5 * * *`). For a new commit on `origin/dev` it runs
`git checkout origin/dev -- server_app rag ingestion processing config_loader.py requirements*.txt Dockerfile.*`
and rebuilds the affected containers. That **replaces uncommitted edits in those paths**.

What happened: a push was followed by the next scheduled run (01:00 at the time), which overwrote uncommitted
work in `rag/`, `ingestion/market_fetcher.py`, `server_app/db.py`, `processing/chunker.py` and
`processing/pdf_parser.py`, and rebuilt the containers from the pushed commit. The agent-side
edits were redone. The three edits that were not mine could not be recovered from git.

It happened a second time the same day (edits made after a push, before the 02:00 run).

Fix (done): `scripts/deploy_dashboard.sh` now checks, before the checkout, whether the paths it
would replace have uncommitted changes (modified or staged tracked files, or an untracked file that
origin also has). If so it deploys nothing, logs `SKIPPED deploy of <sha> ... Files: ...`, and leaves
`ops/.last_deployed_sha` alone so the next run retries. A push that touches no deployed path, and dirty
files outside those paths (`csv_update/` is rewritten all day), are not blocked; `DEPLOY_FORCE=1`
bypasses the check. The cost is that a deploy waits until the work is committed or stashed, so a push
that does not go live should be diagnosed from `ops/deploy.log`.

Practice: still commit what you want kept before the scheduled run, and make further edits in
the deployed paths only after the push has been deployed.

## 6. Open problems and recommended next steps

Ordered by how much they would reduce the problems above.

1. **Verification before display, not after.** Hold back or replace an answer when many figures
   cannot be traced to the tool output; show the source text instead. The grounding check already
   produces the list; only the gate is missing.
2. **A document catalogue and a fixed "read this document" pipeline.** The index stores chunks,
   not documents; titles, dates and file locations are inferred. A catalogue (id, type, title,
   publication date and its origin, source path, size) built from existing chunk metadata would
   make "today's PDF" an exact query. For requests like "read / summarise this document" the steps
   are known, so a program can find the document, load its text and let the model do only the
   summary, instead of letting the model decide each step.
3. **Tool calling.** The hand-written text-and-regex loop (five accepted formats, a repair
   retry) is fragile. A model with reliable native function calling, or a more capable model than
   MiniMax-M2.7-highspeed, would remove a class of failures. The model change is a cost and
   latency decision.
4. **Metadata that is stored but unused.** `page`, `heading` and `has_table` do not influence ranking, and
   `newsletter_path` is never read (`read_email` rebuilds the text from chunks although the original `.md` is on
   disk). The two ingestion paths write different metadata: `rebuild_index.py` adds `heading` and `has_table`,
   `main.py` does not, and PDFs carry no `date` at all (it is derived from the file name at query time).
5. **Smaller items.** Errors travel as text with a prefix instead of a typed state; one shared
   `Orchestrator` behind one lock serialises all chat requests; the chat window renders `$...$`
   as math, which garbles prices written as `$779.82`; the daily call budget of the data provider
   is shared with batch jobs, so each added tool costs calls.
