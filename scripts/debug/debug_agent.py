#!/usr/bin/env python
"""Debug agent tool calling."""
import os
import sys
from pathlib import Path

# Set working directory to project root (financial_rag), two levels up from scripts/debug/
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
os.chdir(_PROJECT_ROOT)
sys.path.insert(0, str(_PROJECT_ROOT))

import yaml
from config_loader import load_config
from rag.embedder import Embedder
from rag.vector_store import FAISSVectorStore
from ingestion.market_fetcher import MarketFetcher
from rag.agent_retriever import AgentRetriever

cfg = load_config(strict=True)

print("Initializing systems...")
embedder = Embedder(cfg['rag']['embedding_model'])
store = FAISSVectorStore(embedder=embedder, dim=embedder.dim)
market_fetcher = MarketFetcher(
    cfg.get('market', {}).get('symbols', []),
    cfg.get('market', {}),
)
agent = AgentRetriever(embedder, store, market_fetcher, cfg)

print("\n=== Testing simple question ===")
print("Question: What is your name?")
try:
    response = agent.query("What is your name?", stream=False)
    print(f"\nResponse:\n{response}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()

print("\n=== Testing tool call (document search) ===")
print("Question: Search documents about stocks")
try:
    response = agent.query("Are there any documents about stocks in my index?", stream=False)
    print(f"\nResponse:\n{response}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
