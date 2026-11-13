# ============================================================
#  eval/adapters/single_agent.py — Adapter for today's single AgentRetriever
#
#  Builds the same Embedder / FAISSVectorStore / MarketFetcher / AgentRetriever
#  stack as query.py --agent (see test_agent_query.py for the reference
#  construction). Tool calls are captured by temporarily wrapping
#  AgentRetriever.tool_map with recording shims — no changes to
#  rag/agent_retriever.py or rag/agent_tools.py are needed.
# ============================================================

from __future__ import annotations

from pathlib import Path

from eval.adapters.base import Trace, ToolCallRecord

REPO_ROOT = Path(__file__).resolve().parents[2]


class _Recorder:
    """Wraps a LangChain @tool object; records each .invoke() call."""

    def __init__(self, name: str, tool_fn, sink: list[ToolCallRecord]):
        self._name = name
        self._tool_fn = tool_fn
        self._sink = sink

    def invoke(self, args):
        result = self._tool_fn.invoke(args)
        self._sink.append(ToolCallRecord(agent="main", tool=self._name, args=dict(args), result=result))
        return result


class SingleAgentAdapter:
    def __init__(self, config_path: str | Path | None = None):
        from config_loader import load_config
        from rag.embedder import Embedder
        from rag.vector_store import FAISSVectorStore
        from rag.agent_retriever import AgentRetriever
        from ingestion.market_fetcher import MarketFetcher

        if config_path is None:
            cfg = load_config(strict=True)
        else:
            import yaml
            cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
        embedder = Embedder(cfg["rag"]["embedding_model"])
        store = FAISSVectorStore(embedder=embedder, dim=embedder.dim)
        market_fetcher = MarketFetcher(cfg.get("market", {}).get("symbols", []), cfg.get("market", {}))

        self.retriever = AgentRetriever(embedder, store, market_fetcher, cfg)

    def run(self, query: str, history: list[dict] | None = None) -> Trace:
        calls: list[ToolCallRecord] = []
        original_map = dict(self.retriever.tool_map)
        self.retriever.tool_map = {
            name: _Recorder(name, fn, calls) for name, fn in original_map.items()
        }
        try:
            if history:
                self.retriever.set_history(history)
            else:
                self.retriever.clear_history()
            try:
                answer = self.retriever.query(query)
                error = None
            except Exception as exc:  # keep the trace even if the agent blows up
                answer = ""
                error = str(exc)
        finally:
            self.retriever.tool_map = original_map

        return Trace(query=query, history=history or [], tool_calls=calls, final_answer=answer, error=error)
