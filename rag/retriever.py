from langchain_core.prompts import PromptTemplate
from langchain_ollama import OllamaLLM
import re
import json
import datetime
import threading
from openai import OpenAI
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from processing.file_reader import find_referenced_file, read_file_for_context
from rag.grounding import check_numeric_grounding, RETRY_SUFFIX_TEMPLATE


class RAGRetriever:
    _LANG_NAMES = {
        "zh": "Chinese",
        "en": "English",
        "de": "German",
    }

    @staticmethod
    def _build_language_instruction(languages: list[str]) -> str:
        names = [RAGRetriever._LANG_NAMES.get(lg, lg) for lg in languages]
        if len(names) == 1:
            return f"- Please answer in {names[0]}, with a clear structure and key data presented numerically:"
        ordered = ", then in ".join(names)
        return f"- Please answer in {ordered}, with a clear structure and key data presented numerically:"

    def __init__(self, embedder: Embedder, store: FAISSVectorStore, cfg: dict):
        self.embedder = embedder
        self.store = store
        self.cfg = cfg
        self.ollama_url = "http://localhost:11434"
        self.model_name = cfg["llm"]["ollama_model"]  # qwen3:8, qwen3.5:latest
        self.top_k = cfg["rag"]["top_k"]
        self.max_context_chunks = max(1, min(5, int(cfg.get("rag", {}).get("context_chunks", self.top_k))))
        self.max_context_chars = max(200, int(cfg.get("rag", {}).get("context_chars", 1800)))

        # Conversation history: keep the last N turns verbatim, and roll everything
        # older than that into a running summary instead of dropping it outright.
        self._history_recent_turns = max(1, int(cfg.get("rag", {}).get("history_recent_turns", 3)))
        self._history_summary_enabled = bool(cfg.get("rag", {}).get("history_summary_enabled", True))
        self._history_summary_max_chars = max(200, int(cfg.get("rag", {}).get("history_summary_max_chars", 800)))
        self._history_summary_timeout_seconds = int(cfg.get("rag", {}).get("history_summary_timeout_seconds", 20))
        self._history_summary: str = ""
        self._history_summarized_upto: int = 0  # index into self.history already folded into the summary

        self.llm = OllamaLLM(
            model=self.model_name,
            base_url=self.ollama_url,
            temperature=0.3,
        )

        # Response language config: default to ["zh", "de"] for backward compatibility
        raw_langs = cfg.get("rag", {}).get("response_languages", ["zh", "de"])
        self._response_languages: list[str] = [lg.strip().lower() for lg in raw_langs if lg.strip()]
        lang_instruction = self._build_language_instruction(self._response_languages)

        self.prompt_tmpl = PromptTemplate.from_template(
            """You are a professional economic intelligence analysis assistant.
            Please answer user questions based only on the retrieved evidence below.
            If the evidence is insufficient, state this clearly; do not fabricate information.

## Retrieved evidence (structured, top-ranked chunks)
{context}

## Recent market data (live quotes fetched in this session)
{market_context}

## Conversation history
{history}

## User question
{question}

## Answer requirements
- Use only the evidence above to answer the question.
- Prefer specific facts and direct quotations from the evidence.
- If the evidence is insufficient, state "Based on the retrieved documents, there is not enough information to answer the question."
- Do not invent numbers, dates, or claims that are not present in the evidence.
- """ + lang_instruction
        )

        # Multi-turn conversation history
        self.history: list[dict] = []

        # Last market quote context (bridged from query.py after /quote commands)
        self._last_market_context: str = ""

        # Routing config
        routing = cfg["llm"].get("routing", {})
        self._cloud_keywords: list[str] = [kw.lower() for kw in routing.get("cloud_keywords", [])]
        self._relevance_threshold: float = float(routing.get("relevance_threshold", 0.20))
        self._force_local_for_market: bool = routing.get("force_local_for_market", True)
        self._enable_route_stats: bool = routing.get("enable_route_stats", False)
        self._route_stats_log_path: str = routing.get("route_stats_log_path", "data/route_stats.jsonl")
        self._route_stats_print_every: int = int(routing.get("route_stats_print_every", 0))
        self._local_timeout_seconds: int = int(routing.get("local_timeout_seconds", 180))
        self._local_timeout_fallback_to_cloud: bool = bool(
            routing.get("local_timeout_fallback_to_cloud", True)
        )
        self._query_count: int = 0

        # Post-generation grounding check: the prompt tells the model not to
        # invent numbers/dates, but nothing enforced that -- this is a cheap
        # rule-based check (see rag/grounding.py), not semantic verification.
        self._grounding_check_enabled: bool = bool(
            cfg.get("rag", {}).get("grounding_check_enabled", True)
        )
        # When the check flags something, regenerate once with the flagged
        # tokens named explicitly, instead of only ever warning after the
        # fact. Same backend that produced the first draft (no local->cloud
        # escalation here -- that's what the timeout fallback is for).
        # Capped so a false-positive heuristic match can't loop forever.
        self._grounding_retry_enabled: bool = bool(
            cfg.get("rag", {}).get("grounding_retry_enabled", True)
        )
        self._grounding_max_retries: int = max(
            0, int(cfg.get("rag", {}).get("grounding_max_retries", 1))
        )

        # Cloud client (MiniMax via OpenAI-compatible API; only if API key is configured)
        minimax_api_key = cfg["llm"].get("minimax_api_key", "").strip()
        self._cloud_model: str = cfg["llm"].get("minimax_model", "MiniMax-M3")
        self._cloud_base_url: str = cfg["llm"].get("minimax_base_url", "https://api.minimaxi.com/v1").strip()
        self._cloud_client = (
            OpenAI(api_key=minimax_api_key, base_url=self._cloud_base_url)
            if minimax_api_key else None
        )

    def query(self, question: str, stream: bool = True, force_cloud: bool = False) -> str:
        """
        Full RAG workflow: Retrieve → Route → Generate (Local or Cloud)
        Routing:
                    Layer 1: cloud_keywords match → cloud model
                    Layer 2: best relevance score < threshold → cloud model
          Default: local Qwen (Ollama)
        """
        # 1. Retrieve documents (a directly-referenced file takes priority over
        #    everything else, then filename query, then semantic search)
        docs = []
        best_score = 0.0
        referenced_path = find_referenced_file(question)
        if referenced_path:
            context = self._format_referenced_file_context(referenced_path)
            best_score = 1.0
        else:
            filename_query = self._extract_pdf_filename(question)
            if filename_query:
                docs = self.store.search_by_filename(filename_query, top_k=self.top_k)
            if not docs:
                docs, best_score = self.store.search_text_with_scores(question, top_k=self.top_k)

            if not docs:
                context = "(There is no relevant document, please run data ingestion first)"
            else:
                context = self._format_context(docs)

        # 2. Determine routing
        is_market_q = self._is_market_question(question)
        use_cloud = False
        route_reason = "default"
        matched_keyword = None

        if force_cloud and self._cloud_client:
            use_cloud = True
            route_reason = "forced cloud"
        elif self._cloud_client:
            # Cloud-keyword match always wins over force_local_for_market.
            # Investment-advice questions (empfehlung/recommend/建议 etc.) always go to the cloud model
            # even when the question also mentions market terms like "Kurs".
            matched_keyword = self._match_cloud_keyword(question)
            if matched_keyword:
                use_cloud = True
                route_reason = f'keyword "{matched_keyword}"'
            elif not (is_market_q and self._force_local_for_market):
                if best_score < self._relevance_threshold:
                    use_cloud = True
                    route_reason = f"low relevance ({best_score:.2f} < {self._relevance_threshold})"

        # 3. Print routing banner
        model_label = self._cloud_model if use_cloud else self.model_name
        backend = "Cloud" if use_cloud else "Local"
        if use_cloud:
            print(f"[Relevance: {best_score:.2f}] → {backend}: {model_label}  ({route_reason})")
        elif is_market_q and self._force_local_for_market:
            print(f"[Relevance: {best_score:.2f}] → {backend}: {model_label}  (market query, forced local)")
        else:
            print(f"[Relevance: {best_score:.2f}] → {backend}: {model_label}")
        print()

        # 4. Build prompt and call model
        prompt = self._build_prompt(question, context)
        final_used_cloud = use_cloud
        final_route_reason = route_reason

        if use_cloud:
            answer = self._call_cloud(prompt, stream=stream)
        else:
            should_timeout_guard = (
                self._local_timeout_fallback_to_cloud
                and self._local_timeout_seconds > 0
                and self._cloud_client is not None
            )
            if should_timeout_guard:
                answer, timed_out = self._call_ollama_with_timeout(prompt, self._local_timeout_seconds)
                if timed_out:
                    final_used_cloud = True
                    final_route_reason = f"local timeout ({self._local_timeout_seconds}s)"
                    print(
                        f"\n[Route Fallback] Local model timed out after "
                        f"{self._local_timeout_seconds}s -> switching to Cloud: {self._cloud_model}\n"
                    )
                    answer = self._call_cloud(prompt, stream=stream)
                elif stream:
                    print(answer)
            else:
                answer = self._call_ollama(prompt, stream=stream)

        # 5. Grounding check: flag numbers/dates the model may have invented,
        #    and regenerate once (same backend) naming exactly what didn't
        #    verify, before falling back to just warning about it.
        if self._grounding_check_enabled and answer and not answer.startswith("❌"):
            evidence_pool = f"{context}\n{self._last_market_context}"
            unverified = check_numeric_grounding(answer, evidence_pool)

            retries_left = self._grounding_max_retries if self._grounding_retry_enabled else 0
            while unverified and retries_left > 0:
                retries_left -= 1
                print(
                    "\n[Grounding Check] ⚠️  Unverified figures: " + ", ".join(unverified)
                    + " — regenerating once...\n"
                )
                retry_prompt = prompt + RETRY_SUFFIX_TEMPLATE.format(unverified=", ".join(unverified))
                retried = (
                    self._call_cloud(retry_prompt, stream=stream) if final_used_cloud
                    else self._call_ollama(retry_prompt, stream=stream)
                )
                if not retried or retried.startswith("❌"):
                    break  # regeneration itself failed -- keep the original draft, just warn below
                answer = retried
                unverified = check_numeric_grounding(answer, evidence_pool)

            if unverified:
                print(
                    "\n[Grounding Check] ⚠️  Not found in retrieved evidence/market data, please verify: "
                    + ", ".join(unverified)
                )

        # 6. Save conversation history
        self.history.append({"role": "user", "content": question})
        self.history.append({"role": "assistant", "content": answer})
        self._update_history_summary()

        # 7. Log route stats
        self._query_count += 1
        self._log_route_stats(question, final_route_reason, final_used_cloud, best_score, matched_keyword)

        return answer

    def _call_ollama_with_timeout(self, prompt: str, timeout_seconds: int) -> tuple[str, bool]:
        """Run local inference in a worker thread and return (answer, timed_out)."""
        box: dict[str, str] = {"answer": ""}

        def _target():
            box["answer"] = self._call_ollama(prompt, stream=False)

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout=max(1, timeout_seconds))
        if worker.is_alive():
            return "", True
        return box.get("answer", ""), False

    @staticmethod
    def _extract_pdf_filename(question: str) -> str | None:
        q = (question or "").strip()
        if not q:
            return None
        # Match both plain and space-containing PDF filenames.
        m = re.search(r"([A-Za-z0-9_()\-\. ]+\.pdf)", q, flags=re.IGNORECASE)
        if not m:
            return None
        return m.group(1).strip()

    def _format_referenced_file_context(self, path) -> str:
        """Read a directly-referenced file fresh from disk and format it as context."""
        text = read_file_for_context(path)
        return f"--- [File] {path.name} ---\n{text}"

    @staticmethod
    def _normalize_chunk_text(text: str) -> str:
        return re.sub(r"\s+", " ", (text or "").strip())

    def _deduplicate_context_docs(self, docs: list[dict]) -> list[dict]:
        seen: set[str] = set()
        deduped: list[dict] = []
        for doc in docs or []:
            text = self._normalize_chunk_text(doc.get("text", ""))
            if not text:
                continue
            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            deduped.append({**doc, "text": text})
        return deduped

    def _prepare_context_docs(self, docs: list[dict]) -> list[dict]:
        deduped = self._deduplicate_context_docs(docs)
        scored: list[tuple[float, dict]] = []
        for doc in deduped:
            raw_score = doc.get("rerank_score")
            try:
                score = float(raw_score) if raw_score is not None else 0.0
            except (TypeError, ValueError):
                score = 0.0
            scored.append((score, doc))

        scored.sort(key=lambda item: item[0], reverse=True)
        ranked = [doc for _, doc in scored]

        if len(ranked) >= 3:
            ordered = [ranked[0]]
            if len(ranked) > 2:
                ordered.extend(ranked[2:])
                ordered.append(ranked[1])
            ranked = ordered

        return ranked[: self.max_context_chunks]

    def _truncate_chunk_text(self, text: str, max_chars: int | None = None) -> str:
        max_chars = max_chars if max_chars is not None else self.max_context_chars
        cleaned = self._normalize_chunk_text(text)
        if len(cleaned) <= max_chars:
            return cleaned
        return cleaned[:max_chars].rstrip() + "..."

    def _format_context(self, docs: list[dict]) -> str:
        if not docs:
            return "(No relevant documents retrieved)"

        prepared_docs = self._prepare_context_docs(docs)
        if not prepared_docs:
            return "(No relevant documents retrieved)"

        parts = [f"Retrieved evidence ({len(prepared_docs)} structured chunk(s))"]
        for idx, doc in enumerate(prepared_docs, 1):
            source = doc.get("source", "unknown")
            if source == "email":
                label = f"[Email] Subject: {doc.get('subject', 'N/A')}"
            elif source == "market":
                label = f"[Market Data] Symbol: {doc.get('symbol', 'N/A')} | Time: {doc.get('timestamp', 'N/A')}"
            elif source == "pdf":
                label = f"[PDF] {doc.get('filename', 'N/A')} Page {doc.get('page', '?')}"
            else:
                label = f"[Document {idx}]"

            parts.append(f"\n### Evidence {idx}/{len(prepared_docs)}")
            parts.append(f"Source: {source}")
            parts.append(f"Metadata: {label}")
            parts.append("Content:")
            parts.append(self._truncate_chunk_text(doc.get("text", "")))

        return "\n".join(parts)

    def set_market_context(self, text: str):
        """Inject live market quote text so it appears in the next RAG prompt."""
        self._last_market_context = (text or "").strip()

    def _build_prompt(self, question: str, context: str) -> str:
        # Most recent turns, kept verbatim
        recent = self.history[-(self._history_recent_turns * 2):]
        history_text = ""
        for msg in recent:
            role = "User" if msg["role"] == "user" else "Assistant"
            history_text += f"{role}: {msg['content']}\n"

        if self._history_summary_enabled and self._history_summary:
            combined_history = (
                "Summary of earlier conversation (entities, preferences, prior conclusions):\n"
                f"{self._history_summary}\n\n"
                "Most recent turns:\n"
                f"{history_text if history_text else '(none)'}"
            )
        else:
            combined_history = history_text if history_text else "（No conversation history）"

        market_ctx = self._last_market_context or "(No market data fetched in this session)"

        return self.prompt_tmpl.format(
            context=context,
            market_context=market_ctx,
            history=combined_history,
            question=question,
        )

    _HISTORY_SUMMARY_PROMPT = """Update the running summary of this conversation.
Keep it short (max ~120 words), as terse bullet points. Preserve concrete facts:
named entities (companies, tickers, ISIN/WKN), user preferences or goals, and any
conclusions already given. Drop small talk and anything not useful for future turns.

## Existing summary
{existing_summary}

## New exchange to fold in
{new_turns}

## Updated summary (bullet points only, no preamble)
"""

    def _fold_history_into_summary(self, existing_summary: str, new_turns: list[dict]) -> str | None:
        """Ask the local model to merge new_turns into existing_summary, with a
        timeout so a slow/stuck Ollama can't delay the caller. Returns None on
        failure/timeout so the caller can retry the same turns next time."""
        turns_text = ""
        for msg in new_turns:
            role = "User" if msg["role"] == "user" else "Assistant"
            turns_text += f"{role}: {msg['content']}\n"

        prompt = self._HISTORY_SUMMARY_PROMPT.format(
            existing_summary=existing_summary or "(none yet)",
            new_turns=turns_text,
        )

        box: dict[str, str] = {"summary": ""}

        def _target():
            try:
                box["summary"] = str(self.llm.invoke(prompt))
            except Exception as e:
                print(f"[History Summary] error: {e}")

        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout=max(1, self._history_summary_timeout_seconds))
        if worker.is_alive():
            print(f"[History Summary] timed out after {self._history_summary_timeout_seconds}s, keeping old summary")
            return None

        summary = box["summary"].strip()
        return summary or None

    def _update_history_summary(self):
        """Fold any turns about to fall out of the recent window into the
        running summary. Called once per query, after the answer is appended
        to history, so it never adds latency to the visible answer."""
        if not self._history_summary_enabled:
            return

        window_start = max(0, len(self.history) - self._history_recent_turns * 2)
        if window_start <= self._history_summarized_upto:
            return

        to_fold = self.history[self._history_summarized_upto:window_start]
        if not to_fold:
            return

        updated = self._fold_history_into_summary(self._history_summary, to_fold)
        if updated is None:
            return  # keep old summary, retry folding these same turns next time

        if len(updated) > self._history_summary_max_chars:
            updated = updated[: self._history_summary_max_chars].rstrip() + "..."

        self._history_summary = updated
        self._history_summarized_upto = window_start

    _MARKET_KEYWORDS = (
        "stock", "etf", "isin", "quote", "price", "market",
        "股价", "价格", "行情", "报价", "股票", "基金",
        "aktie", "aktienkurs", "kurs", "preis", "markt",
    )

    def _is_market_question(self, question: str) -> bool:
        q = (question or "").lower()
        return any(k in q for k in self._MARKET_KEYWORDS)

    def _match_cloud_keyword(self, question: str) -> str | None:
        q = (question or "").lower()
        for kw in self._cloud_keywords:
            if kw in q:
                return kw
        return None

    def _call_cloud(self, prompt: str, stream: bool = True) -> str:
        full_response = ""
        try:
            if stream:
                stream_resp = self._cloud_client.chat.completions.create(
                    model=self._cloud_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=2048,
                    extra_body={"thinking": {"type": "disabled"}},
                    stream=True,
                )
                for chunk in stream_resp:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    text = delta.content or ""
                    if text:
                        full_response += text
                        print(text, end="", flush=True)
                print()
            else:
                msg = self._cloud_client.chat.completions.create(
                    model=self._cloud_model,
                    messages=[{"role": "user", "content": prompt}],
                    max_completion_tokens=2048,
                    extra_body={"thinking": {"type": "disabled"}},
                )
                full_response = msg.choices[0].message.content or ""
        except Exception as e:
            full_response = f"❌ MiniMax API error: {e}"
        return full_response

    def _log_route_stats(self, question: str, reason: str, used_cloud: bool, score: float, keyword: str | None):
        if not self._enable_route_stats:
            return
        normalized_score = None if score is None else round(float(score), 4)
        event = {
            "timestamp": datetime.datetime.now().isoformat(),
            "question": question[:120],
            "route": "cloud" if used_cloud else "local",
            "reason": reason,
            "relevance_score": normalized_score,
            "model": self._cloud_model if used_cloud else self.model_name,
            "keyword": keyword,
        }
        try:
            with open(self._route_stats_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass
        if self._route_stats_print_every > 0 and self._query_count % self._route_stats_print_every == 0:
            self._print_route_summary()

    def _print_route_summary(self):
        try:
            with open(self._route_stats_log_path, encoding="utf-8") as f:
                events = [json.loads(line) for line in f if line.strip()]
        except (OSError, json.JSONDecodeError):
            return
        if not events:
            return
        total = len(events)
        cloud = sum(1 for e in events if e.get("route") == "cloud")
        local = total - cloud
        print(f"\n── Route Stats (last {total} queries) ─────────────")
        print(f"  Local : {local} ({100*local//total}%)  |  Cloud: {cloud} ({100*cloud//total}%)")
        print("──────────────────────────────────────────────────\n")

    def _call_ollama(self, prompt: str, stream: bool = True) -> str:
        full_response = ""

        try:
            if stream:
                for chunk in self.llm.stream(prompt):
                    token = str(chunk)
                    full_response += token
                    print(token, end="", flush=True)
                print()
            else:
                full_response = str(self.llm.invoke(prompt))
        except ConnectionError:
            full_response = "❌ Unable to connect to Ollama, please ensure it is running: ollama serve"
        except Exception as e:
            error_text = str(e)
            if "not found" in error_text and "model" in error_text:
                full_response = (
                    f"❌ Ollama model not found: {self.model_name}."
                    " Please run `ollama list` to check local models, "
                    "then update llm.ollama_model in config.yaml to an installed model, "
                    "or run `ollama pull <model_name>` to download the required model."
                )
            else:
                full_response = f"❌ Inference error: {e}"
        
        return full_response

    def clear_history(self):
        self.history = []
        self._history_summary = ""
        self._history_summarized_upto = 0
        print("Conversation history cleared")