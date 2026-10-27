"""Layer F · Insights & Signals.

Question it answers: which business signals should we prepare and reuse?
Work: define once, compute and store, version and refresh, serve.

Without this layer every application calls the fraud engine, which recomputes a score
from raw tables on each call and never sees the graph. Here the features are computed
once (two of them over the knowledge graph), scored by a versioned model, stored with
their inputs and compute time, and served to every consumer from gold.claim_signals.
"""

import json
import math
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import yaml

from claims import config, lakehouse, stores

# fraud-model v3.2: a logistic model. Coefficients are the model artefact; in production
# they would come from the model registry, here they are fixed so the demo is reproducible.
FRAUD_MODEL = {"version": "3.2", "intercept": -4.084,
               "weights": {"shared_contact_claims_30d": 0.55, "shop_claims_30d": 0.15,
                           "contact_phone_not_claimant": 0.9, "post_loss_endorsement": 1.2},
               "caps": {"shared_contact_claims_30d": 5, "shop_claims_30d": 8}}


def fraud_score(features: dict) -> float:
    z = FRAUD_MODEL["intercept"]
    for name, w in FRAUD_MODEL["weights"].items():
        v = float(features.get(name) or 0)
        z += w * min(v, FRAUD_MODEL["caps"].get(name, v))
    return round(1 / (1 + math.exp(-z)), 2)


GRAPH_FEATURES = """
MATCH (c:Claim)
OPTIONAL MATCH (c)-[:CONTACT_PHONE]->(ph:Phone)<-[:CONTACT_PHONE]-(o:Claim)
  WHERE o <> c AND abs(duration.inDays(o.filed_date, c.filed_date).days) <= 30
WITH c, count(DISTINCT o) AS shared_contact
OPTIONAL MATCH (c)-[:REPAIRED_AT]->(s:RepairShop)<-[:REPAIRED_AT]-(o2:Claim)
  WHERE o2 <> c AND abs(duration.inDays(o2.filed_date, c.filed_date).days) <= 30
WITH c, shared_contact, count(DISTINCT o2) AS shop_claims
OPTIONAL MATCH (c)-[:CONTACT_PHONE]->(cp:Phone)
OPTIONAL MATCH (c)-[:FILED_BY]->(:Person)-[:HAS_PHONE]->(own:Phone)
WITH c, shared_contact, shop_claims, collect(DISTINCT cp.e164) AS contact, collect(DISTINCT own.e164) AS own
RETURN c.id AS claim_id, shared_contact AS shared_contact_claims_30d, shop_claims AS shop_claims_30d,
       CASE WHEN size(contact) > 0 AND size(own) > 0 AND none(x IN contact WHERE x IN own) THEN 1 ELSE 0 END AS contact_phone_not_claimant
"""


def build_features() -> pd.DataFrame:
    graph = pd.DataFrame(stores.cypher(GRAPH_FEATURES))
    claims = lakehouse.read("silver.claims")
    pv = lakehouse.read("silver.policy_versions")
    rows = []
    for c in claims.to_dict("records"):
        versions = pv[pv.policy_id == c["policy_id"]]
        loss = pd.Timestamp(c["loss_date"])
        filed_end = pd.Timestamp(c["filed_date"]) + pd.Timedelta(days=2)
        post_loss = int(((versions.change_type == "ENDORSE") & (versions.valid_from > loss) & (versions.valid_from <= filed_end)).any())
        issue = versions[versions.version == 1]
        age = int((loss - pd.Timestamp(issue.iloc[0]["valid_from"])).days) if len(issue) else None
        rows.append({"claim_id": c["claim_id"], "post_loss_endorsement": post_loss, "policy_age_days": age})
    df = pd.DataFrame(rows).merge(graph, on="claim_id", how="left").fillna(
        {"shared_contact_claims_30d": 0, "shop_claims_30d": 0, "contact_phone_not_claimant": 0})
    for col in ("shared_contact_claims_30d", "shop_claims_30d", "contact_phone_not_claimant"):
        df[col] = df[col].astype(int)
    df["computed_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    lakehouse.write("gold.claim_features", df)
    return df


def build_benchmarks() -> pd.DataFrame:
    claims = lakehouse.read("silver.claims")
    pv = lakehouse.read("silver.policy_versions").drop_duplicates("policy_id")[["policy_id", "segment"]]
    settled = claims[claims.approved_amount.notna()].merge(pv, on="policy_id")
    rows = []
    for (seg, lt, band), g in settled.groupby(["segment", "loss_type", "severity_band"]):
        a = g.approved_amount.to_numpy()
        rows.append({"segment": seg, "loss_type": lt, "severity_band": band, "n": len(a),
                     "p10": float(np.percentile(a, 10)), "p50": float(np.percentile(a, 50)), "p90": float(np.percentile(a, 90))})
    df = pd.DataFrame(rows)
    df["computed_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    lakehouse.write("gold.settlement_benchmarks", df)
    return df


def _round_lakh(x: float) -> float:
    return round(x / 10000) * 10000


def build_signals(features: pd.DataFrame | None = None, benchmarks: pd.DataFrame | None = None) -> pd.DataFrame:
    spec = yaml.safe_load((config.CATALOG_AS_CODE / "signals.yaml").read_text())["signals"]
    features = features if features is not None else lakehouse.read("gold.claim_features")
    benchmarks = benchmarks if benchmarks is not None else lakehouse.read("gold.settlement_benchmarks")
    claims = lakehouse.read("silver.claims")
    seg = lakehouse.read("silver.policy_versions").drop_duplicates("policy_id").set_index("policy_id")["segment"]
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for f in features.to_dict("records"):
        feats = {k: f[k] for k in spec["fraud_score"]["features"]}
        rows.append({"claim_id": f["claim_id"], "signal": "fraud_score", "value": fraud_score(feats), "value_low": None,
                     "value_high": None, "version": spec["fraud_score"]["version"],
                     "model_version": f"fraud-model {FRAUD_MODEL['version']}", "computed_at": now,
                     "valid_until": now + pd.Timedelta(hours=spec["fraud_score"]["freshness_sla_hours"]),
                     "inputs": json.dumps({k: (None if v is None else int(v)) for k, v in feats.items()})})
    for c in claims[claims.status == "OPEN"].to_dict("records"):
        b = benchmarks[(benchmarks.segment == seg.get(c["policy_id"])) & (benchmarks.loss_type == c["loss_type"])
                       & (benchmarks.severity_band == c["severity_band"])]
        if not len(b):
            continue
        b = b.iloc[0]
        rows.append({"claim_id": c["claim_id"], "signal": "settlement_range", "value": _round_lakh(b.p50),
                     "value_low": _round_lakh(b.p10), "value_high": _round_lakh(b.p90),
                     "version": spec["settlement_range"]["version"], "model_version": f"benchmark n={int(b.n)}",
                     "computed_at": now, "valid_until": now + pd.Timedelta(hours=spec["settlement_range"]["freshness_sla_hours"]),
                     "inputs": json.dumps({"segment": b.segment, "loss_type": b.loss_type, "severity_band": b.severity_band, "n": int(b.n)})})
    df = pd.DataFrame(rows)
    lakehouse.write("gold.claim_signals", df)
    return df


def link_signals_to_graph(signals: pd.DataFrame | None = None) -> int:
    signals = signals if signals is not None else lakehouse.read("gold.claim_signals")
    stores.cypher("MATCH (s:Inst:Signal) DETACH DELETE s")
    stores.cypher("""UNWIND $rows AS r MATCH (c:Claim {id:r.claim_id})
        MERGE (s:Inst:Signal {id: r.claim_id + ':' + r.signal}) SET s.name=r.signal, s.value=r.value, s.low=r.value_low,
            s.high=r.value_high, s.version=r.version, s.model=r.model_version, s.computed_at=r.computed_at
        MERGE (c)-[:HAS_SIGNAL]->(s)
        WITH s MATCH (d:Dataset {name:'gold.claim_signals'}) MERGE (s)-[:RECORDED_IN]->(d)""",
                  rows=[{**r, "computed_at": str(r["computed_at"])} for r in signals.to_dict("records")])
    return len(signals)
