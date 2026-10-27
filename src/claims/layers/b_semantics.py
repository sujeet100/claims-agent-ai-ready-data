"""Layer B · Metadata & Semantics.

Question it answers: do we understand what those records mean?
Work: describe fields, define terms with owners, map across systems.

The catalog-as-code in catalog/ is the source of truth for meaning. This layer checks it
against the real table schemas, asks a model to draft any missing column meanings (AI for
Data, marked needs_review), writes the metadata subgraph (systems, datasets, columns,
terms, owners, lineage) and builds the discovery index the agent searches first.
"""

import pandas as pd
import yaml
from pydantic import BaseModel
from qdrant_client import models

from claims import config, lakehouse, llm, stores


def _yaml(name: str) -> dict:
    return yaml.safe_load((config.CATALOG_AS_CODE / name).read_text())


class ColumnMeaning(BaseModel):
    column: str
    description: str


class ColumnMeanings(BaseModel):
    columns: list[ColumnMeaning]


def build_catalog(use_llm: bool = True) -> dict:
    spec = _yaml("datasets.yaml")
    glossary = _yaml("glossary.yaml")["terms"]
    present = set(lakehouse.tables(("silver", "gold")))
    datasets, columns, gaps = [], [], []
    for ds, meta in spec["datasets"].items():
        datasets.append({"dataset": ds, "owner": meta["owner"], "description": meta["description"],
                         "upstream": ",".join(meta.get("upstream", [])), "materialized": ds in present})
        declared = meta.get("columns", {}) or {}
        # meaning is declared before a table exists, like a contract; real schemas are checked when they do
        actual = [f.name for f in lakehouse.catalog().load_table(ds).schema().fields] if ds in present else list(declared)
        for col in actual:
            c = declared.get(col) or {}
            row = {"dataset": ds, "column": col, "description": c.get("description"), "term": c.get("term"),
                   "classification": c.get("classification"), "source": "catalog-as-code", "review_status": "approved"}
            if not row["description"] and ds in present:
                gaps.append(row)
            columns.append(row)

    # AI for Data: draft meanings for undocumented columns from the table's purpose and sample values
    drafted = 0
    if gaps and use_llm:
        by_ds: dict[str, list[dict]] = {}
        for g in gaps:
            by_ds.setdefault(g["dataset"], []).append(g)
        for ds, rows in by_ds.items():
            sample = lakehouse.read(ds).head(5)[[r["column"] for r in rows]].to_dict("records")
            prompt = (f"Table {ds}: {spec['datasets'][ds]['description']}\n"
                      f"Columns needing a one-sentence business meaning: {[r['column'] for r in rows]}\n"
                      f"Sample rows: {sample}")
            out, how = llm.structured("column_meanings", "You write concise data catalog column descriptions for an "
                                      "insurance claims platform. One sentence each, plain language, no speculation "
                                      "beyond what the name, table purpose and samples support.", prompt, ColumnMeanings)
            if out:
                meaning = {m.column: m.description for m in out.columns}
                for r in rows:
                    if r["column"] in meaning:
                        r.update(description=meaning[r["column"]], source=f"ai-drafted ({how})", review_status="needs_review")
                        drafted += 1

    terms = [{"term": t, "owner": v["owner"], "definition": " ".join(v["definition"].split()),
              "synonyms": ",".join(v.get("synonyms", []))} for t, v in glossary.items()]
    lakehouse.write("meta.datasets", pd.DataFrame(datasets))
    lakehouse.write("meta.columns", pd.DataFrame(columns))
    lakehouse.write("meta.glossary", pd.DataFrame(terms))
    return {"datasets": len(datasets), "columns": len(columns), "terms": len(terms),
            "undocumented": len(gaps), "ai_drafted": drafted}


def build_metadata_graph() -> dict:
    """Systems, datasets, columns, glossary terms, owners and lineage, as graph nodes and edges."""
    spec = _yaml("datasets.yaml")
    apis = _yaml("apis.yaml")["apis"]
    cols = lakehouse.read("meta.columns").to_dict("records")
    terms = lakehouse.read("meta.glossary").to_dict("records")
    stores.cypher("MATCH (n:Meta) DETACH DELETE n")
    for name, s in spec["systems"].items():
        stores.cypher("MERGE (s:Meta:System {name:$name}) SET s.kind=$kind "
                      "MERGE (t:Meta:Team {name:$owner}) MERGE (s)-[:OWNED_BY]->(t)",
                      name=name, kind=s["kind"], owner=s["owner"])
    for t in terms:
        stores.cypher("MERGE (g:Meta:GlossaryTerm {name:$term}) SET g.definition=$definition, g.synonyms=$synonyms "
                      "MERGE (o:Meta:Team {name:$owner}) MERGE (g)-[:OWNED_BY]->(o)", **t)
    for ds, meta in spec["datasets"].items():
        stores.cypher("MERGE (d:Meta:Dataset {name:$ds}) SET d.description=$desc, d.layer=$layer "
                      "MERGE (o:Meta:Team {name:$owner}) MERGE (d)-[:OWNED_BY]->(o)",
                      ds=ds, desc=meta["description"], layer=ds.split(".")[0], owner=meta["owner"])
        for up in meta.get("upstream", []):
            stores.cypher("MERGE (d:Meta:Dataset {name:$ds}) MERGE (u:Meta:Dataset {name:$up}) "
                          "SET u.layer = coalesce(u.layer, split($up,'.')[0]) MERGE (d)-[:DERIVED_FROM]->(u)", ds=ds, up=up)
    # bronze tables come from source systems
    for bronze, system in [("bronze.claims_db_claims", "Claims DB"), ("bronze.claims_db_assessments", "Claims DB"),
                           ("bronze.claims_sheet", "Regional Tracker"), ("bronze.policy_audit_log", "Policy Admin"),
                           ("bronze.crm_customers", "CRM"), ("bronze.garage_registry", "Garage Registry"),
                           ("bronze.document_inventory", "Uploads")]:
        stores.cypher("MERGE (d:Meta:Dataset {name:$d}) SET d.layer='bronze' WITH d MATCH (s:Meta:System {name:$s}) "
                      "MERGE (d)-[:INGESTED_FROM]->(s)", d=bronze, s=system)
    stores.cypher("""UNWIND $cols AS c
        MATCH (d:Meta:Dataset {name:c.dataset})
        MERGE (col:Meta:Column {key: c.dataset + '.' + c.column})
        SET col.name=c.column, col.description=c.description, col.review_status=c.review_status, col.source=c.source
        MERGE (d)-[:HAS_COLUMN]->(col)
        WITH col, c WHERE c.term IS NOT NULL
        MATCH (g:Meta:GlossaryTerm {name:c.term}) MERGE (col)-[:MEANS]->(g)""", cols=cols)
    for key, a in apis.items():
        stores.cypher("MERGE (a:Meta:APIEndpoint {name:$key}) SET a.path=$path, a.description=$desc "
                      "WITH a MATCH (s:Meta:System {name:$system}) MERGE (a)-[:PROVIDED_BY]->(s) "
                      "WITH a MATCH (g:Meta:GlossaryTerm {name:$serves}) MERGE (a)-[:SERVES_LIVE]->(g)",
                      key=key, path=a["path"], desc=a["description"], system=a["system"], serves=a["serves"])
    n = stores.cypher("MATCH (n:Meta) RETURN count(n) AS n")[0]["n"]
    e = stores.cypher("MATCH (:Meta)-[r]->(:Meta) RETURN count(r) AS n")[0]["n"]
    return {"metadata_nodes": n, "metadata_edges": e}


# What the discovery plane can route to. The agent searches these by meaning before it picks a tool.
CAPABILITIES = [
    ("tool", "query_lakehouse", "Run read-only SQL over governed lakehouse tables: claims, policy versions as of a date, customers, documents register, signals."),
    ("tool", "search_documents", "Search FIRs, repair invoices and survey reports by meaning; returns passages with document, page and section to cite."),
    ("tool", "investigate_links", "Follow the knowledge graph from a claim to related claims through shared repair shops, contact phones, vehicles and people, with the evidence for each link."),
    ("tool", "get_signals", "Read precomputed, versioned signals for a claim: fraud_score and settlement_range."),
    ("tool", "get_policy_status", "Live status of a policy right now, from the policy admin API. Current view only."),
    ("tool", "define_term", "Look up the owned business definition of a term such as severity band, approved amount or fast-track."),
    ("tool", "submit_decision", "Submit a recommendation or action on a claim through the governed gateway."),
]


def build_discovery_index() -> dict:
    stores.recreate_collection("discovery")
    entries = []
    for d in lakehouse.read("meta.datasets").to_dict("records"):
        cols = lakehouse.read("meta.columns")
        cols = cols[cols.dataset == d["dataset"]]
        col_text = "; ".join(f"{c.column}: {c.description}" for c in cols.itertuples() if isinstance(c.description, str))
        entries.append({"kind": "dataset", "name": d["dataset"], "route": "query_lakehouse",
                        "text": f"Dataset {d['dataset']} (owner {d['owner']}). {d['description']} Columns: {col_text}"})
    for t in lakehouse.read("meta.glossary").to_dict("records"):
        entries.append({"kind": "term", "name": t["term"], "route": "define_term",
                        "text": f"Business term {t['term']} (owner {t['owner']}): {t['definition']} Also called: {t['synonyms']}"})
    for key, a in _yaml("apis.yaml")["apis"].items():
        entries.append({"kind": "api", "name": key, "route": "get_policy_status" if key == "policy_status" else "get_signals",
                        "text": f"API {a['path']} from {a['system']}: {a['description']}"})
    for key, s in _yaml("signals.yaml")["signals"].items():
        entries.append({"kind": "signal", "name": key, "route": "get_signals",
                        "text": f"Signal {key} v{s['version']} (owner {s['owner']}): {s['description']}"})
    for kind, name, text in CAPABILITIES:
        entries.append({"kind": kind, "name": name, "route": name, "text": f"Capability {name}: {text}"})
    vectors = stores.embed([e["text"] for e in entries])
    stores.qdrant().upsert("discovery", points=[models.PointStruct(id=i, vector=v, payload=e)
                                                for i, (e, v) in enumerate(zip(entries, vectors))])
    return {"discovery_entries": len(entries)}
