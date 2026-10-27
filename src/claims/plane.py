"""The unified discovery plane: the one surface an agent reaches the data through.

Each tool belongs to the layer that makes it possible, and a session at a given stage only
sees the tools its layers provide. "before" is the agent-first build from the talk: a SQL
agent over the raw claims database, the policy status API and a document tool that
classifies but never reads. Stages A to F add one layer each, cumulatively.

The same functions back the in-process agent (claims.agent) and the MCP server
(claims.mcp_server), so governance, tracing and audit behave identically for both.
"""

import inspect
import re
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import wraps
from typing import Any, Callable

import yaml
from pydantic_ai import RunContext, Tool

from claims import config, governance, lakehouse, sources, stores
from claims.layers.c_retrieval import _stable_id

STAGES = ["before", "A", "B", "C", "D", "E", "F"]
LAYER_NAMES = {"A": "Enterprise Data Organization", "B": "Metadata & Semantics", "C": "Retrieval-Ready Knowledge",
               "D": "Governance & Control", "E": "Knowledge Integration", "F": "Insights & Signals"}

STAGE_TABLES = {
    "A": ["silver.claims", "silver.policy_versions", "silver.customers", "silver.documents", "silver.repair_shops",
          "silver.quality_events"],
    "B": ["meta.datasets", "meta.columns", "meta.glossary"],
    "C": ["silver.document_chunks"],
    "D": ["meta.classifications", "meta.access_policies"],
    "E": [],
    "F": ["gold.claim_features", "gold.claim_signals", "gold.settlement_benchmarks"],
}


def has(stage: str, layer: str) -> bool:
    return stage != "before" and STAGES.index(stage) >= STAGES.index(layer)


@dataclass
class Session:
    stage: str = "F"
    principal: governance.Principal = field(default_factory=governance.Principal)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    trace: list[dict] = field(default_factory=list)
    decisions: list[dict] = field(default_factory=list)
    _duck: Any = None
    _redactor: Any = None
    _lock: Any = field(default_factory=threading.RLock)

    @property
    def governed(self) -> bool:
        return has(self.stage, "D")

    @property
    def redactor(self) -> governance.Redactor | None:
        with self._lock:
            if self.governed and self._redactor is None:
                self._redactor = governance.Redactor.for_principal(self.principal)
        return self._redactor

    def tables(self) -> list[str]:
        return [t for layer, ts in STAGE_TABLES.items() if has(self.stage, layer) for t in ts if lakehouse.exists(t)]

    def duck(self):
        with self._lock:   # the model may call several tools at once; build the session only once
            if self._duck is None:
                self._duck = (governance.governed_session(self.redactor, self.tables()) if self.governed
                              else lakehouse.duck(self.tables()))
        return self._duck

    def sql(self, query: str, params: list | None = None):
        """Run a query on its own cursor: one DuckDB connection is not safe across threads."""
        return self.duck().cursor().execute(query, params or []).fetchdf()

    def text(self, value: str | None) -> str | None:
        return self.redactor.text(value) if self.governed else value

    def phone(self, e164: str | None, masked: str | None = None) -> str | None:
        if self.governed and self.principal.role != "investigator":
            return masked or (self.redactor.phone(e164) if e164 else None)
        return e164

    def person(self, name: str | None, key: str | None) -> str | None:
        return self.redactor.name(name, key) if self.governed else name


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, float) and v != v:
        return None
    return v


def _rows(df, limit=50) -> list[dict]:
    return [{k: _jsonable(v) for k, v in r.items()} for r in df.head(limit).to_dict("records")]


# ------------------------------------------------------------------------ the registry

REGISTRY: list[tuple[Callable, str, str, str]] = []   # (fn, available from, available until, backend)


def tool(available_from: str, backend: str, until: str = "F"):
    def deco(fn):
        REGISTRY.append((fn, available_from, until, backend))
        return fn
    return deco


def available(stage: str) -> list[tuple[Callable, str]]:
    out = []
    for fn, frm, until, backend in REGISTRY:
        if frm == "before":
            ok = STAGES.index(stage) <= STAGES.index(until)
        else:
            ok = has(stage, frm) and STAGES.index(stage) <= STAGES.index(until)
        if ok:
            out.append((fn, backend))
    return out


MASKED = re.compile(r"•••|\bPerson [0-9A-F]{6}\b")


def traced(s: Session, fn: Callable, backend: str, kwargs: dict) -> Any:
    started = time.perf_counter()
    try:
        result = fn(s, **kwargs)
        error = None
    except Exception as e:  # tool errors go back to the model, as a SQL error would
        result, error = {"error": f"{type(e).__name__}: {e}"}, str(e)
    ms = int((time.perf_counter() - started) * 1000)
    out = json.dumps(result, default=_jsonable, ensure_ascii=False)
    masked = len(MASKED.findall(out)) if s.governed else 0   # masked values that reached the model in this result
    entry = {"step": len(s.trace) + 1, "tool": fn.__name__, "args": kwargs, "backend": backend, "ms": ms,
             "masked": masked, "error": error, "output": out}
    s.trace.append(entry)
    if s.governed:
        governance.audit({"run_id": s.run_id, "user": s.principal.user, "role": s.principal.role, "stage": s.stage,
                          "tool": fn.__name__, "args": kwargs, "backend": backend, "masked_values": masked,
                          "datasets": governance.touched(kwargs.get("sql", "")) if "sql" in kwargs else [],
                          "error": error})
    return result


def _strip_first(fn: Callable) -> inspect.Signature:
    sig = inspect.signature(fn)
    return sig.replace(parameters=list(sig.parameters.values())[1:])


def pydantic_tools(stage: str) -> list[Tool]:
    """PydanticAI tools for a stage. The session travels as the run's deps."""
    tools = []
    for fn, backend in available(stage):
        def make(fn=fn, backend=backend):
            @wraps(fn)
            def call(ctx: RunContext[Session], **kwargs):
                return traced(ctx.deps, fn, backend, kwargs)
            sig = _strip_first(fn)
            call.__signature__ = sig.replace(parameters=[inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                                                           annotation=RunContext[Session])] + list(sig.parameters.values()))
            call.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "s"} | {"ctx": RunContext[Session]}
            return Tool(call, takes_ctx=True, name=fn.__name__)
        tools.append(make())
    return tools


def bound_functions(session_factory: Callable[[], Session], stage: str = "F") -> list[Callable]:
    """Plain functions with the session bound, for the MCP server."""
    out = []
    for fn, backend in available(stage):
        def make(fn=fn, backend=backend):
            @wraps(fn)
            def call(**kwargs):
                return traced(session_factory(), fn, backend, kwargs)
            call.__signature__ = _strip_first(fn)
            call.__annotations__ = {k: v for k, v in fn.__annotations__.items() if k != "s"}
            return call
        out.append(make())
    return out


# ------------------------------------------------------------- before: the first build

@tool("before", "Claims DB · Postgres (raw)", until="before")
def list_tables(s: Session) -> list[dict]:
    """List the tables in the claims database with their columns and a few sample rows."""
    return sources.raw_schema()


@tool("before", "Claims DB · Postgres (raw)", until="before")
def run_sql(s: Session, sql: str) -> dict:
    """Run a read-only SQL query on the claims database (Postgres). Schemas: claims_db, crm.

    Args:
        sql: A single SELECT statement.
    """
    return sources.run_raw_sql(sql)


@tool("before", "Uploads folder", until="before")
def list_uploads(s: Session) -> list[str]:
    """List the files in the claim documents uploads folder."""
    return sorted(p.name for p in config.UPLOADS.glob("*.pdf"))


@tool("before", "Document classifier", until="B")
def classify_document(s: Session, file_name: str) -> dict:
    """Classify an uploaded claim document by type (FIR, invoice, survey). Does not extract its content.

    Args:
        file_name: The uploaded file's name.
    """
    from claims.layers.a_organization import _doc_type
    from claims.layers.c_retrieval import pdf_pages
    path = config.UPLOADS / file_name
    if not path.exists():
        return {"error": "no such file"}
    return {"file_name": file_name, "doc_type": _doc_type("\n".join(pdf_pages(path)))}


@tool("before", "Policy Admin API (live)")
def get_policy_status(s: Session, policy_id: str) -> dict:
    """Get the live status and terms of a policy from the policy admin system, as they stand right now.
    This is the current view only; it does not say what applied on a past date.

    Args:
        policy_id: Policy number, e.g. P-1234.
    """
    return sources.policy_status(policy_id)


# ------------------------------------------------------------------ A: organization

@tool("A", "Lakehouse · Iceberg + DuckDB")
def list_datasets(s: Session) -> list[dict]:
    """List the governed lakehouse datasets available to you, with their columns (and meanings, where the catalog has them)."""
    meanings = {}
    if has(s.stage, "B"):
        cols = lakehouse.read("meta.columns")
        meanings = {(r.dataset, r.column): r.description for r in cols.itertuples() if isinstance(r.description, str)}
        ds_desc = dict(zip(lakehouse.read("meta.datasets").dataset, lakehouse.read("meta.datasets").description))
    out = []
    for t in s.tables():
        cols = list(s.sql(f"DESCRIBE {t}").column_name)
        entry = {"dataset": t, "columns": {c: meanings.get((t, c), "") for c in cols} if meanings else cols}
        if meanings:
            entry["description"] = ds_desc.get(t, "")
        out.append(entry)
    return out


@tool("A", "Lakehouse · Iceberg + DuckDB")
def query_lakehouse(s: Session, sql: str) -> dict:
    """Run read-only DuckDB SQL over the governed lakehouse tables (schemas silver, gold, meta).

    Args:
        sql: A single SELECT statement, e.g. SELECT * FROM silver.claims WHERE claim_id = 'CLM-12345'.
    """
    if not sql.strip().lower().startswith(("select", "with")):
        return {"error": "only SELECT queries are allowed"}
    df = s.sql(sql)
    return {"rows": _rows(df), "row_count": len(df), "governed": s.governed}


@tool("A", "Lakehouse · silver.policy_versions")
def get_policy_terms(s: Session, policy_id: str, as_of_date: str) -> dict:
    """Get the terms of a policy that were in force on a given date, from the policy's full version history,
    plus any later changes to it.

    Args:
        policy_id: Policy number, e.g. P-1234.
        as_of_date: The date that matters, e.g. the loss date, as YYYY-MM-DD.
    """
    cols = ("policy_id, version, valid_from, valid_to, change_type, endorsement_id, product, make_model, vehicle_reg, "
            "segment, idv, zero_dep, deductible, term_start, term_end, status")
    as_of = f"CAST('{as_of_date}' AS DATE) + INTERVAL 1 DAY"
    in_force = s.sql(f"SELECT {cols} FROM silver.policy_versions WHERE policy_id = ? AND valid_from < {as_of} "
                     "ORDER BY version DESC LIMIT 1", [policy_id])
    later = s.sql(f"SELECT version, valid_from, change_type, endorsement_id, zero_dep, deductible "
                  f"FROM silver.policy_versions WHERE policy_id = ? AND valid_from >= {as_of} ORDER BY version", [policy_id])
    terms = _rows(in_force)[0] if len(in_force) else None
    changes = _rows(later)
    summary = None
    if terms:
        summary = (f"On {as_of_date} version {terms['version']} was in force: zero-depreciation add-on "
                   f"{'IN FORCE' if terms['zero_dep'] else 'NOT in force'}, deductible Rs {terms['deductible']:,.0f}.")
        for c in changes:
            summary += f" Changed on {str(c['valid_from'])[:10]} ({c['change_type']} {c['endorsement_id'] or ''}): zero_dep={c['zero_dep']}."
    return {"policy_id": policy_id, "as_of_date": as_of_date, "summary": summary, "in_force": terms, "changes_after": changes}


@tool("A", "Lakehouse · silver.documents")
def list_claim_documents(s: Session, claim_id: str) -> list[dict]:
    """List the registered documents for a claim, with type, version, whether it is the latest, and owner.

    Args:
        claim_id: Claim ID, e.g. CLM-12345.
    """
    df = s.sql("SELECT doc_id, file_name, doc_type, version, is_latest, owner, pages FROM silver.documents "
               "WHERE claim_id = ? ORDER BY doc_type, version", [claim_id])
    return _rows(df)


# ---------------------------------------------------------------------- B: semantics

@tool("B", "Discovery index · Qdrant")
def search_catalog(s: Session, query: str) -> list[dict]:
    """Search the catalog and discovery index by meaning: datasets, business terms, APIs, signals and which tool answers what.

    Args:
        query: What you are looking for, in plain words.
    """
    hits = stores.qdrant().query_points("discovery", query=stores.embed([query])[0], limit=6).points
    allowed = {fn.__name__ for fn, _ in available(s.stage)}
    return [{"kind": h.payload["kind"], "name": h.payload["name"], "route_to_tool": h.payload["route"]
             if h.payload["route"] in allowed else None, "text": h.payload["text"][:600], "score": round(h.score, 3)} for h in hits]


@tool("B", "Catalog · glossary")
def define_term(s: Session, term: str) -> dict:
    """Get the owned business definition of a term, e.g. 'severity band', 'approved amount', 'fast-track'.

    Args:
        term: The business term.
    """
    g = lakehouse.read("meta.glossary")
    t = term.lower()
    hit = g[g.term.str.lower().eq(t) | g.synonyms.str.lower().str.contains(t, regex=False)]
    if not len(hit):
        hit = g[g.term.str.lower().str.contains(t.split()[0], regex=False)]
    return _rows(hit, 3) or [{"error": f"no definition for '{term}'"}]


# --------------------------------------------------------------------- C: retrieval

@tool("C", "Vector index · Qdrant")
def search_documents(s: Session, query: str, claim_id: str | None = None, limit: int = 5) -> list[dict]:
    """Search FIRs, repair invoices and survey reports by meaning. Returns passages with document, page and section to cite.

    Args:
        query: What to look for, in plain words.
        claim_id: Optional claim ID to restrict the search to one claim's documents.
        limit: Number of passages (max 8).
    """
    from qdrant_client import models
    flt = models.Filter(must=[models.FieldCondition(key="claim_id", match=models.MatchValue(value=claim_id))]) if claim_id else None
    hits = stores.qdrant().query_points("documents", query=stores.embed([query])[0], query_filter=flt, limit=min(limit, 8)).points
    return [{"cite": f"{h.payload['file_name']} · page {h.payload['page']} · {h.payload['section']}",
             "doc_id": h.payload["doc_id"], "chunk_id": h.payload["chunk_id"], "claim_id": h.payload["claim_id"],
             "doc_type": h.payload["doc_type"], "text": s.text(h.payload["text"]), "score": round(h.score, 3)} for h in hits]


# -------------------------------------------------------------------- D: governance

@tool("D", "Catalog · lineage graph")
def trace_lineage(s: Session, dataset: str) -> dict:
    """Trace where a dataset comes from: its upstream datasets back to the source systems, and its owner.

    Args:
        dataset: Dataset name, e.g. gold.claim_signals.
    """
    rows = stores.cypher("""MATCH p = (d:Dataset {name:$d})-[:DERIVED_FROM|INGESTED_FROM*1..6]->(x)
                            RETURN [n IN nodes(p) | n.name] AS path ORDER BY length(p) DESC LIMIT 8""", d=dataset)
    owner = stores.cypher("MATCH (:Dataset {name:$d})-[:OWNED_BY]->(t) RETURN t.name AS owner", d=dataset)
    return {"dataset": dataset, "owner": owner[0]["owner"] if owner else None, "paths": [r["path"] for r in rows]}


# ------------------------------------------------------------------- E: integration

LINKS = """
MATCH (c:Claim {id:$id})-[r1:REPAIRED_AT|CONTACT_PHONE]->(hub)<-[r2:REPAIRED_AT|CONTACT_PHONE]-(o:Claim)
WHERE o <> c AND abs(duration.inDays(o.filed_date, c.filed_date).days) <= $window
OPTIONAL MATCH (o)-[:FILED_BY]->(p:Person)
OPTIONAL MATCH (o)-[:HAS_DOC]->(d:Document)-[m:MENTIONS]->(hub)
RETURN o.id AS claim_id, toString(o.filed_date) AS filed_date, o.requested AS requested, o.severity_band AS band,
       p.name AS claimant, p.key AS claimant_key, type(r2) AS via, labels(hub) AS hub_labels,
       coalesce(hub.name, hub.e164) AS hub, hub.masked AS hub_masked, hub.id AS hub_id,
       collect(DISTINCT {doc: d.file, page: m.page, chunk_id: m.chunk_id, raw: m.raw, method: m.method,
                         rule: m.rule, confidence: m.confidence}) AS evidence
ORDER BY filed_date DESC
"""

OWN = """
MATCH (c:Claim {id:$id})-[r:REPAIRED_AT|CONTACT_PHONE]->(hub)
OPTIONAL MATCH (c)-[:HAS_DOC]->(d:Document)-[m:MENTIONS]->(hub)
OPTIONAL MATCH (hub)-[:STORED_IN]->(:Column)-[:CLASSIFIED_AS]->(cls:Classification)-[:VISIBLE_TO]->(role:Role)
RETURN type(r) AS via, coalesce(hub.name, hub.e164) AS hub, hub.masked AS hub_masked,
       collect(DISTINCT {doc: d.file, page: m.page, raw: m.raw, rule: m.rule, confidence: m.confidence}) AS evidence,
       collect(DISTINCT cls.name) AS classified_as, collect(DISTINCT role.name) AS visible_to
"""


@tool("E", "Knowledge graph · Neo4j (+ GraphRAG passages)")
def investigate_links(s: Session, claim_id: str, window_days: int = 30) -> dict:
    """Follow the knowledge graph from a claim to other claims that share its repair shop or contact phone within a time window,
    with the evidence for every link and the cited document passages (GraphRAG).

    Args:
        claim_id: Claim ID, e.g. CLM-12345.
        window_days: Days either side of the claim's filing date (default 30).
    """
    own = stores.cypher(OWN, id=claim_id)
    rows = stores.cypher(LINKS, id=claim_id, window=window_days)
    hubs = []
    for h in own:
        is_phone = h["via"] == "CONTACT_PHONE"
        hubs.append({"link": h["via"], "entity": s.phone(h["hub"], h["hub_masked"]) if is_phone else h["hub"],
                     "evidence_on_this_claim": [{**e, "raw": s.text(e["raw"])} for e in h["evidence"] if e["doc"]],
                     "classified_as": h["classified_as"], "visible_to": h["visible_to"]})
    linked: dict[str, dict] = {}
    chunk_ids = set()
    for r in rows:
        entry = linked.setdefault(r["claim_id"], {"claim_id": r["claim_id"], "filed_date": r["filed_date"],
                                                  "requested_inr": r["requested"], "severity_band": r["band"],
                                                  "claimant": s.person(r["claimant"], r["claimant_key"]), "shares": [], "evidence": []})
        is_phone = r["via"] == "CONTACT_PHONE"
        entry["shares"].append({"via": r["via"], "entity": s.phone(r["hub"], r["hub_masked"]) if is_phone else r["hub"]})
        for e in r["evidence"]:
            if e["doc"]:
                entry["evidence"].append({**e, "raw": s.text(e["raw"])})
                chunk_ids.add(e["chunk_id"])
    # GraphRAG: pull the cited passages from the vector index by the chunk IDs the graph holds
    passages = {}
    if chunk_ids:
        points = stores.qdrant().retrieve("documents", ids=[_stable_id(c) for c in chunk_ids], with_payload=True)
        passages = {p.payload["chunk_id"]: s.text(p.payload["text"]) for p in points}
    for entry in linked.values():
        entry["passages"] = [{"chunk_id": e["chunk_id"], "cite": f"{e['doc']} · page {e['page']}", "text": passages.get(e["chunk_id"])}
                             for e in entry["evidence"][:2]]
    return {"claim_id": claim_id, "window_days": window_days, "this_claim_links": hubs,
            "linked_claims": list(linked.values()), "linked_count": len(linked),
            "note": "Shared shop or phone is a pattern to investigate, not proof of fraud."}


# ----------------------------------------------------------------------- F: signals

@tool("F", "Signal store · gold.claim_signals")
def get_signals(s: Session, claim_id: str) -> list[dict]:
    """Read the precomputed, versioned signals for a claim: fraud_score (with its model version and inputs) and settlement_range.

    Args:
        claim_id: Claim ID, e.g. CLM-12345.
    """
    df = s.sql("SELECT * FROM gold.claim_signals WHERE claim_id = ?", [claim_id])
    spec = yaml.safe_load((config.CATALOG_AS_CODE / "signals.yaml").read_text())["signals"]
    rows = _rows(df)
    for r in rows:
        r["definition"] = spec.get(r["signal"], {}).get("description")
        r["owner"] = spec.get(r["signal"], {}).get("owner")
        if r["signal"] == "fraud_score":
            r["threshold"] = spec["fraud_score"]["threshold"]
    return rows


# -------------------------------------------------------------- every stage: action

DECISION_ACTIONS = {"fast_track": "fast_track_settlement", "standard_settlement": "approve_settlement",
                    "hold_and_route_to_investigation": "route_to_investigation",
                    "request_more_information": "request_documents"}


@tool("before", "Action gateway")
def submit_decision(s: Session, claim_id: str, decision: str, amount_inr: int | None = None, rationale: str = "") -> dict:
    """Submit your decision on a claim. decision is one of: fast_track, standard_settlement,
    hold_and_route_to_investigation, request_more_information.

    Args:
        claim_id: Claim ID.
        decision: One of fast_track, standard_settlement, hold_and_route_to_investigation, request_more_information.
        amount_inr: Settlement amount in rupees, for settlement decisions.
        rationale: One or two sentences on why.
    """
    action = DECISION_ACTIONS.get(decision)
    if action is None:
        return {"error": f"decision must be one of {list(DECISION_ACTIONS)}"}
    if s.governed:
        gate = governance.gate_action(action, s.principal)
    elif action in ("fast_track_settlement", "approve_settlement"):
        gate = {"allowed": True, "status": "queued_for_payout",
                "reason": f"queued for automatic payout of Rs {amount_inr or 0:,} in the next disbursal run"}
    else:
        gate = {"allowed": True, "status": "done", "reason": f"{action} recorded"}
    record = {"claim_id": claim_id, "decision": decision, "action": action, "amount_inr": amount_inr,
              "rationale": rationale, **gate}
    s.decisions.append(record)
    return record
