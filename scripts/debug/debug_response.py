#!/usr/bin/env python
"""Debug agent response format."""
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, str(_PROJECT_ROOT))

import yaml
from config_loader import load_config
from rag.agent_retriever import AgentRetriever
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from ingestion.market_fetcher import MarketFetcher
from langchain_core.messages import SystemMessage, HumanMessage

cfg = load_config(strict=True)
embedder = Embedder(cfg['rag']['embedding_model'])
store = FAISSVectorStore(embedder=embedder, dim=embedder.dim)
market_fetcher = MarketFetcher(cfg.get('market', {}).get('symbols', []), cfg.get('market', {}))

agent = AgentRetriever(embedder, store, market_fetcher, cfg)

# Test LLM response structure
messages = [
    SystemMessage(content="Du bist ein Assistent."),
    HumanMessage(content="Was ist in meinem Dokumentenindex?"),
]

response = agent.llm.invoke(messages)
print(f"Response type: {type(response)}")
print(f"Response content: {response.content[:200]}")
print(f"Has tool_calls attr: {hasattr(response, 'tool_calls')}")
if hasattr(response, "tool_calls"):
    print(f"tool_calls: {response.tool_calls}")
print(f"\nFull response attributes: {dir(response)}")
