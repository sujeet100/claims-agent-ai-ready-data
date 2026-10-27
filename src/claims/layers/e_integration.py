"""Layer E · Knowledge Integration: the enterprise knowledge graph.

Question it answers: what connections across sources affect the decision?
Work: identify entities, resolve duplicates, relate with evidence.

The graph is built in three passes, and each pass is kept as a lakehouse table so the
graph can always be rebuilt and audited:

1. Mentions: every place an entity appears, in a record (claims DB row, CRM record,
   registry row) or in a document (FIR page 2, invoice header), with its raw text.
2. Resolution: each mention is resolved to one entity by a named rule with a confidence
   (E.164 phone, GSTIN, normalised name + fuzzy match, CRM identity resolution).
3. Relation: entities are linked, and every edge carries its evidence (source system or
   document, page, chunk, method, confidence). Claim-level links such as REPAIRED_AT and
   CONTACT_PHONE are derived from document mentions and cite them.

The instance graph is then bridged to the metadata and governance graph from layers B and
D: a claim is RECORDED_IN silver.claims, a phone extracted from a FIR is STORED_IN the
chunk text column, which is CLASSIFIED_AS PII.Phone, which is VISIBLE_TO investigators.
One traversal can answer "what links this claim, who says so, and who may see it".
"""

from datetime import datetime, timezone

import pandas as pd
from rapidfuzz import fuzz

from claims import lakehouse, stores
from claims.normalize import mask_phone, phone_e164, shop_key

RUN_AT = lambda: datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")  # noqa: E731


def collect_mentions() -> pd.DataFrame:
    rows = []

    def add(entity_type, raw, source, locator, claim_id=None, page=None, chunk_id=None, method="record", **extra):
        rows.append({"entity_type": entity_type, "raw_value": str(raw), "source": source, "locator": locator,
                     "claim_id": claim_id, "page": page, "chunk_id": chunk_id, "method": method,
                     "gstin_hint": extra.get("gstin_hint"), "customer_hint": extra.get("customer_hint")})

    for c in lakehouse.read("silver.claims").to_dict("records"):
        add("Claim", c["claim_id"], c["sources"], f"silver.claims#{c['claim_id']}", c["claim_id"])
    for p in lakehouse.read("silver.customers").to_dict("records"):
        add("Phone", p["phone_e164"], "CRM", f"silver.customers#{p['customer_key']}", customer_hint=p["customer_key"])
    for s in lakehouse.read("silver.repair_shops").to_dict("records"):
        add("RepairShop", s["name"], "Garage Registry", f"silver.repair_shops#{s['shop_id']}", gstin_hint=s["shop_id"])

    claims = lakehouse.read("silver.claims").set_index("claim_id")
    fields = lakehouse.read("silver.document_fields")
    gstin_of_doc = fields[fields.field == "gstin"].set_index("doc_id")["value"].to_dict()
    for f in fields.to_dict("records"):
        chunk = f"{f['doc_id']}-p{f['page']}"
        hint = claims["customer_key"].get(f["claim_id"]) if f["claim_id"] in claims.index else None
        loc = f"{f['doc_id']} page {f['page']}"
        if f["field"] == "contact_phone":
            add("Phone", f["value"], f["doc_id"], loc, f["claim_id"], f["page"], chunk, f["method"])
        elif f["field"] == "repair_shop":
            add("RepairShop", f["value"], f["doc_id"], loc, f["claim_id"], f["page"], chunk, f["method"],
                gstin_hint=gstin_of_doc.get(f["doc_id"]))
        elif f["field"] in ("complainant", "customer"):
            add("Person", f["value"], f["doc_id"], loc, f["claim_id"], f["page"], chunk, f["method"], customer_hint=hint)
        elif f["field"] == "vehicle_reg":
            add("Vehicle", f["value"], f["doc_id"], loc, f["claim_id"], f["page"], chunk, f["method"])
    df = pd.DataFrame(rows)
    df.insert(0, "mention_id", [f"M{i:05d}" for i in range(len(df))])
    lakehouse.write("silver.entity_mentions", df)
    return df


def resolve_shop(raw: str, gstin_hint: str | None, registry: pd.DataFrame) -> tuple[str | None, str, float]:
    if gstin_hint and gstin_hint in set(registry.shop_id):
        return gstin_hint, "GSTIN exact match", 1.0
    key = shop_key(raw)
    best, score = None, 0.0
    for s in registry.itertuples():
        sc = fuzz.token_set_ratio(key, shop_key(s.name))
        if sc > score:
            best, score = s, sc
    if best is None or score < 85:
        return None, "no registry match", score / 100
    surface = fuzz.ratio(raw.lower(), best.name.lower()) / 100
    return best.shop_id, "normalised name + fuzzy match", round(0.85 + 0.14 * surface, 2)


def resolve_mentions(mentions: pd.DataFrame | None = None) -> pd.DataFrame:
    mentions = mentions if mentions is not None else lakehouse.read("silver.entity_mentions")
    registry = lakehouse.read("silver.repair_shops")
    customers = lakehouse.read("silver.customers").set_index("customer_key")
    out = []
    for m in mentions.to_dict("records"):
        t, raw = m["entity_type"], m["raw_value"]
        if t == "Claim":
            eid, rule, conf = raw, "canonical claim ID", 1.0
        elif t == "Phone":
            e = phone_e164(raw)
            eid, rule, conf = (f"PHONE:{e}", "E.164 normalisation", 1.0) if e else (None, "unparseable phone", 0.0)
        elif t == "RepairShop":
            sid, rule, conf = resolve_shop(raw, m["gstin_hint"], registry)
            eid = f"SHOP:{sid}" if sid else f"SHOP?:{shop_key(raw)}"
        elif t == "Person":
            key = m["customer_hint"]
            known = customers["full_name"].get(key) if key in customers.index else None
            sc = fuzz.token_sort_ratio(raw.lower(), (known or "").lower()) / 100 if known else 0
            eid, rule, conf = (f"PERSON:{key}", "claimant of the claim + name match", round(sc, 2)) if sc >= 0.8 else (None, "unmatched name", sc)
        elif t == "Vehicle":
            eid, rule, conf = f"VEHICLE:{raw.replace(' ', '').upper()}", "registration normalisation", 1.0
        else:
            eid, rule, conf = None, "unknown type", 0.0
        out.append({"mention_id": m["mention_id"], "entity_id": eid, "rule": rule, "confidence": float(conf)})
    df = pd.DataFrame(out)
    lakehouse.write("silver.entity_resolution", df)
    return df


def _edge(source, locator, method, confidence, run, **extra) -> dict:
    return {"source": source, "locator": locator, "method": method, "confidence": float(confidence), "run_at": run, **extra}


def build_graph() -> dict:
    run = RUN_AT()
    stores.cypher("MATCH (n:Inst) DETACH DELETE n")
    for label, key in [("Claim", "id"), ("Person", "key"), ("Phone", "e164"), ("Vehicle", "reg"), ("Policy", "id"),
                       ("RepairShop", "id"), ("Document", "doc_id"), ("SourceRecord", "id")]:
        stores.cypher(f"CREATE CONSTRAINT {label.lower()}_key IF NOT EXISTS FOR (n:{label}) REQUIRE n.{key} IS UNIQUE")

    claims = lakehouse.read("silver.claims")
    customers = lakehouse.read("silver.customers")
    smap = lakehouse.read("silver.customer_source_map")
    pv = lakehouse.read("silver.policy_versions").sort_values("version").drop_duplicates("policy_id", keep="last")
    shops = lakehouse.read("silver.repair_shops")
    docs = lakehouse.read("silver.documents")
    mentions = lakehouse.read("silver.entity_mentions").merge(lakehouse.read("silver.entity_resolution"), on="mention_id")

    # --- entities from records ---------------------------------------------------------
    stores.cypher("""UNWIND $rows AS r MERGE (p:Inst:Person {key:r.customer_key})
        SET p.name=r.full_name, p.city=r.city, p.resolution_rule=r.resolution_rule, p.resolution_confidence=r.resolution_confidence
        WITH p, r WHERE r.phone_e164 IS NOT NULL
        MERGE (ph:Inst:Phone {e164:r.phone_e164}) SET ph.masked=r.masked
        MERGE (p)-[e:HAS_PHONE]->(ph) SET e += r.ev""",
                  rows=[{**c, "masked": mask_phone(c["phone_e164"]),
                         "ev": _edge("CRM", f"silver.customers#{c['customer_key']}", "record", 1.0, run)}
                        for c in customers.to_dict("records")])
    stores.cypher("""UNWIND $rows AS r MERGE (s:Inst:SourceRecord {id:r.id}) SET s.system=r.system, s.name_in_source=r.name
        WITH s, r MATCH (p:Person {key:r.key}) MERGE (s)-[e:RESOLVES_TO]->(p) SET e.rule=r.rule, e.confidence=r.confidence, e.run_at=$run""",
                  rows=[{"id": f"crm:{m['source_id']}", "system": "CRM", "name": m["name_in_source"], "key": m["customer_key"],
                         "rule": m["rule"], "confidence": float(m["confidence"])} for m in smap.to_dict("records")], run=run)
    stores.cypher("""UNWIND $rows AS r MERGE (s:Inst:RepairShop {id:r.shop_id}) SET s.name=r.name, s.city=r.city, s.address=r.address
        MERGE (ph:Inst:Phone {e164:r.phone_e164}) SET ph.masked=r.masked, ph.business=true
        MERGE (s)-[e:HAS_PHONE]->(ph) SET e += r.ev""",
                  rows=[{**s, "masked": mask_phone(s["phone_e164"]),
                         "ev": _edge("Garage Registry", f"silver.repair_shops#{s['shop_id']}", "record", 1.0, run)}
                        for s in shops.to_dict("records")])
    stores.cypher("""UNWIND $rows AS r MERGE (p:Inst:Policy {id:r.policy_id})
        SET p.segment=r.segment, p.make_model=r.make_model, p.zero_dep_now=r.zero_dep, p.versions=r.version
        MERGE (v:Inst:Vehicle {reg:r.vehicle_reg}) SET v.make_model=r.make_model
        MERGE (p)-[i:INSURES]->(v) SET i += r.ev
        WITH p, r MATCH (h:Person {key:r.holder_customer_key}) MERGE (p)-[e:HELD_BY]->(h) SET e += r.ev""",
                  rows=[{**p, "version": int(p["version"]),
                         "ev": _edge("Policy Admin", f"silver.policy_versions#{p['policy_id']}", "record", 1.0, run)}
                        for p in pv.to_dict("records")])
    claim_rows = []
    for c in claims.to_dict("records"):
        claim_rows.append({"id": c["claim_id"], "filed": str(c["filed_date"]), "loss": str(c["loss_date"]),
                           "status": c["status"], "requested": c["requested_amount"],
                           "band": c["severity_band"] if isinstance(c["severity_band"], str) else None,
                           "policy": c["policy_id"], "person": c["customer_key"], "reg": c["vehicle_reg"],
                           "ev": _edge(c["sources"], f"silver.claims#{c['claim_id']}", "record", 1.0, run)})
    stores.cypher("""UNWIND $rows AS r MERGE (c:Inst:Claim {id:r.id})
        SET c.filed_date=date(r.filed), c.loss_date=date(r.loss), c.status=r.status, c.requested=r.requested, c.severity_band=r.band
        WITH c, r OPTIONAL MATCH (p:Policy {id:r.policy}) FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END | MERGE (c)-[e:COVERED_BY]->(p) SET e += r.ev)
        WITH c, r OPTIONAL MATCH (h:Person {key:r.person}) FOREACH (_ IN CASE WHEN h IS NULL THEN [] ELSE [1] END | MERGE (c)-[e:FILED_BY]->(h) SET e += r.ev)
        WITH c, r OPTIONAL MATCH (v:Vehicle {reg:r.reg}) FOREACH (_ IN CASE WHEN v IS NULL THEN [] ELSE [1] END | MERGE (c)-[e:INVOLVES]->(v) SET e += r.ev)""",
                  rows=claim_rows)
    stores.cypher("""UNWIND $rows AS r MATCH (c:Claim {id:r.claim_id})
        MERGE (d:Inst:Document {doc_id:r.doc_id}) SET d.type=r.doc_type, d.file=r.file_name, d.version=r.version, d.latest=r.is_latest, d.owner=r.owner
        MERGE (c)-[e:HAS_DOC]->(d) SET e.source='silver.documents', e.run_at=$run""",
                  rows=[{**d, "version": int(d["version"]), "is_latest": bool(d["is_latest"])} for d in docs.to_dict("records")], run=run)

    # --- entities and links from documents, with evidence --------------------------------
    doc_mentions = mentions[mentions.method != "record"]
    link_rows = []
    for m in doc_mentions.to_dict("records"):
        if not m["entity_id"]:
            continue
        link_rows.append({"doc": m["source"], "claim": m["claim_id"], "type": m["entity_type"], "eid": m["entity_id"],
                          "ev": _edge(m["source"], m["locator"], m["method"], m["confidence"], run, page=int(m["page"]),
                                      chunk_id=m["chunk_id"], raw=m["raw_value"], rule=m["rule"])})
    phone_links = [r | {"e164": r["eid"][6:], "masked": mask_phone(r["eid"][6:])} for r in link_rows if r["type"] == "Phone"]
    stores.cypher("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc}), (c:Claim {id:r.claim})
        MERGE (ph:Inst:Phone {e164:r.e164}) SET ph.masked=r.masked
        MERGE (d)-[m:MENTIONS]->(ph) SET m += r.ev
        MERGE (c)-[l:CONTACT_PHONE]->(ph) SET l += r.ev""", rows=phone_links)
    shop_links = [r | {"sid": r["eid"][5:]} for r in link_rows if r["type"] == "RepairShop" and r["eid"].startswith("SHOP:")]
    stores.cypher("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc}), (c:Claim {id:r.claim}), (s:RepairShop {id:r.sid})
        MERGE (d)-[m:MENTIONS]->(s) SET m += r.ev
        MERGE (c)-[l:REPAIRED_AT]->(s)
        SET l.confidence = CASE WHEN l.confidence IS NULL OR r.ev.confidence > l.confidence THEN r.ev.confidence ELSE l.confidence END,
            l.evidence = coalesce(l.evidence, []) + [r.ev.locator], l.chunk_ids = coalesce(l.chunk_ids, []) + [r.ev.chunk_id],
            l.raw = coalesce(l.raw, []) + [r.ev.raw], l.method = r.ev.method, l.run_at = r.ev.run_at""", rows=shop_links)
    person_links = [r | {"key": r["eid"][7:]} for r in link_rows if r["type"] == "Person"]
    stores.cypher("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc}), (p:Person {key:r.key})
        MERGE (d)-[m:MENTIONS]->(p) SET m += r.ev""", rows=person_links)
    vehicle_links = [r | {"reg": r["eid"][8:]} for r in link_rows if r["type"] == "Vehicle"]
    stores.cypher("""UNWIND $rows AS r MATCH (d:Document {doc_id:r.doc}) MERGE (v:Inst:Vehicle {reg:r.reg})
        MERGE (d)-[m:MENTIONS]->(v) SET m += r.ev""", rows=vehicle_links)

    bridge_to_metadata()
    counts = stores.cypher("MATCH (n:Inst) UNWIND [l IN labels(n) WHERE l <> 'Inst'] AS label RETURN label, count(*) AS n ORDER BY n DESC")
    edges = stores.cypher("MATCH (:Inst)-[r]->() RETURN count(r) AS n")[0]["n"]
    return {"nodes": {c["label"]: c["n"] for c in counts}, "instance_edges": edges}


def bridge_to_metadata():
    """Link instances to where they are stored, what they mean and how they are governed."""
    for label, dataset in [("Claim", "silver.claims"), ("Person", "silver.customers"), ("Policy", "silver.policy_versions"),
                           ("Document", "silver.documents"), ("RepairShop", "silver.repair_shops")]:
        stores.cypher(f"MATCH (n:Inst:{label}), (d:Dataset {{name:$d}}) MERGE (n)-[:RECORDED_IN]->(d)", d=dataset)
    for label, term in [("Claim", "Claim"), ("RepairShop", "Repair Shop"), ("Policy", "Policy")]:
        stores.cypher(f"MATCH (n:Inst:{label}), (g:GlossaryTerm {{name:$t}}) MERGE (n)-[:INSTANCE_OF]->(g)", t=term)
    # where each phone value lives, so its classification and visibility can be reached from the instance
    stores.cypher("""MATCH (p:Person)-[:HAS_PHONE]->(ph:Phone), (col:Column {key:'silver.customers.phone_e164'})
                     MERGE (ph)-[:STORED_IN]->(col)""")
    stores.cypher("""MATCH (:Document)-[:MENTIONS]->(ph:Phone), (col:Column {key:'silver.document_chunks.text'})
                     MERGE (ph)-[:STORED_IN]->(col)""")
    stores.cypher("""MATCH (ph:Phone)<-[:CONTACT_PHONE]-(), (g:GlossaryTerm {name:'Contact Phone'}) MERGE (ph)-[:INSTANCE_OF]->(g)""")
    stores.cypher("""MATCH (p:Policy), (a:APIEndpoint {name:'policy_status'}) MERGE (p)-[:LIVE_VIEW]->(a)""")
