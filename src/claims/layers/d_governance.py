"""Layer D · Governance & Control (build time).

Question it answers: what may this user access, and what may the agent do?
Work: classify personal data, trace lineage, evaluate each request, gate actions.

This layer turns policies.yaml into data: classifications on columns and on document
chunks, access rules per role, and governance edges in the graph (column CLASSIFIED_AS
PII.Phone, PII.Phone VISIBLE_TO investigator, an action REQUIRES_APPROVAL_BY a role).
Enforcement happens at request time in claims.governance, inside the SQL session,
inside retrieval and at the action gate.
"""

import re

import pandas as pd
import yaml

from claims import config, lakehouse, stores
from claims.normalize import PHONE

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")


def policies() -> dict:
    return yaml.safe_load((config.CATALOG_AS_CODE / "policies.yaml").read_text())


def classify() -> pd.DataFrame:
    """Declared classifications from the catalog, plus detection over sample values."""
    cols = lakehouse.read("meta.columns")
    rows = []
    for c in cols.to_dict("records"):
        if isinstance(c["classification"], str):
            rows.append({"asset": f"{c['dataset']}.{c['column']}", "classification": c["classification"],
                         "detected_by": "catalog declaration", "confidence": 1.0})
            continue
        sample = lakehouse.read(c["dataset"])[c["column"]].dropna().astype(str).head(50)
        if len(sample) and sample.map(lambda v: bool(PHONE.fullmatch(v.strip()))).mean() > 0.6:
            rows.append({"asset": f"{c['dataset']}.{c['column']}", "classification": "PII.Phone",
                         "detected_by": "pattern scan", "confidence": 0.9})
        elif len(sample) and sample.map(lambda v: bool(EMAIL.fullmatch(v.strip()))).mean() > 0.6:
            rows.append({"asset": f"{c['dataset']}.{c['column']}", "classification": "PII.Email",
                         "detected_by": "pattern scan", "confidence": 0.9})

    # document chunks: which ones carry phones or customer names
    chunks = lakehouse.read("silver.document_chunks")
    names = set(lakehouse.read("silver.customers")["full_name"])
    for ch in chunks.to_dict("records"):
        found = []
        if PHONE.search(ch["text"]):
            found.append("PII.Phone")
        if any(n in ch["text"] for n in names):
            found.append("PII.Name")
        for f in found:
            rows.append({"asset": f"chunk:{ch['chunk_id']}", "classification": f, "detected_by": "pattern scan", "confidence": 0.95})
    df = pd.DataFrame(rows)
    lakehouse.write("meta.classifications", df)
    return df


def tag_chunks(classifications: pd.DataFrame) -> int:
    """Write classifications onto every vector payload so retrieval can enforce them."""
    tagged = classifications[classifications.asset.str.startswith("chunk:")]
    by_chunk = tagged.groupby(tagged.asset.str[6:])["classification"].apply(list).to_dict()
    client = stores.qdrant()
    points, _ = client.scroll("documents", limit=10_000, with_payload=["chunk_id"])
    for p in points:
        client.set_payload("documents", payload={"governed": True, "classifications": by_chunk.get(p.payload["chunk_id"], [])},
                           points=[p.id])
    return len(points)


def compile_policies() -> pd.DataFrame:
    p = policies()
    rows = [{"kind": "classification", "name": k, "visible_to": ",".join(v["visible_to"]), "mask": v["mask"],
             "rule": v.get("officer_rule"), "approver_role": None} for k, v in p["classifications"].items()]
    rows += [{"kind": "action", "name": k, "visible_to": ",".join(v["allowed_for"]), "mask": None,
              "rule": "requires_approval" if v["requires_approval"] else "allowed", "approver_role": v.get("approver_role")}
             for k, v in p["actions"].items()]
    df = pd.DataFrame(rows)
    lakehouse.write("meta.access_policies", df)
    return df


def build_governance_graph(classifications: pd.DataFrame) -> dict:
    p = policies()
    stores.cypher("MATCH (n:Gov) DETACH DELETE n")
    for role, r in p["roles"].items():
        stores.cypher("MERGE (r:Meta:Gov:Role {name:$role}) SET r.description=$d", role=role, d=r["description"])
    for cls, c in p["classifications"].items():
        stores.cypher("MERGE (c:Meta:Gov:Classification {name:$cls}) SET c.mask=$mask", cls=cls, mask=c["mask"])
        for role in c["visible_to"]:
            stores.cypher("MATCH (c:Classification {name:$cls}), (r:Role {name:$role}) MERGE (c)-[:VISIBLE_TO]->(r)", cls=cls, role=role)
    for action, a in p["actions"].items():
        stores.cypher("MERGE (a:Meta:Gov:Action {name:$a}) SET a.requires_approval=$ra", a=action, ra=a["requires_approval"])
        if a.get("approver_role"):
            stores.cypher("MATCH (a:Action {name:$a}), (r:Role {name:$r}) MERGE (a)-[:REQUIRES_APPROVAL_BY]->(r)", a=action, r=a["approver_role"])
    cols = classifications[~classifications.asset.str.startswith("chunk:")].to_dict("records")
    stores.cypher("""UNWIND $rows AS r MATCH (col:Column {key:r.asset}), (c:Classification {name:r.classification})
                     MERGE (col)-[x:CLASSIFIED_AS]->(c) SET x.detected_by=r.detected_by, x.confidence=r.confidence""", rows=cols)
    return {"classified_columns": len(cols),
            "classified_chunks": int(classifications.asset.str.startswith("chunk:").sum())}
