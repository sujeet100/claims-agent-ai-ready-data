"""One place for paths, connection strings and the demo's fixed facts."""

import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
SOURCES = DATA / "sources"            # the "before" world: files that sit beside the operational DBs
UPLOADS = SOURCES / "uploads"         # claim documents as they arrive: no owner, no version
INCOMING = DATA / "incoming"          # where the AI-for-Data demo drops the seventh claim's FIR
WAREHOUSE = DATA / "lakehouse"        # Iceberg warehouse (System of Record)
CATALOG_DB = WAREHOUSE / "catalog.db" # Iceberg SQL catalog (SQLite)
AUDIT_LOG = DATA / "audit" / "audit.jsonl"
RUNS = DATA / "runs"                  # recorded agent runs, replayable when the network is not
RECORDED = DATA / "recorded"          # recorded LLM extractions for offline builds
GROUND_TRUTH = DATA / "generated" / "ground_truth.json"
CATALOG_AS_CODE = ROOT / "catalog"    # glossary, dataset meanings, policies, signal definitions

PG_DSN = os.getenv("PG_DSN", "postgresql://ops:ops-pass@localhost:5440/insurer_ops")
NEO4J_URI = os.getenv("NEO4J_URI", "bolt://localhost:7690")
NEO4J_USER = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "claims-graph")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6340")
OPENMETADATA_URL = os.getenv("OPENMETADATA_URL", "http://localhost:8585")
APP_URL = os.getenv("APP_URL", "http://localhost:8010")

EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")


def default_model() -> str:
    """Claude when an Anthropic key is present, otherwise OpenAI. CLAIMS_MODEL overrides both."""
    if os.getenv("CLAIMS_MODEL"):
        return os.environ["CLAIMS_MODEL"]
    if os.getenv("ANTHROPIC_API_KEY"):
        return "anthropic:claude-sonnet-5-5"
    return "openai:gpt-5.6-luna"   # cheapest GPT-5.6 tier; as accurate as the larger models on this task


def llm_available() -> bool:
    return bool(os.getenv("ANTHROPIC_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("CLAIMS_MODEL"))


# The demo clock. The prompt says the claim was "filed yesterday".
TODAY = date(2026, 10, 15)
CASE_CLAIM = "CLM-88421"
CASE_PROMPT = (
    "Assess claim #CLM-88421, a ₹4.2L car-collision claim filed yesterday. "
    "Should we fast-track settlement, and at what amount? And flag anything suspicious."
)
