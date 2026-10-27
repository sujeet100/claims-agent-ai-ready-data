"""AI for Data: the seventh claim, a week later.

A new claim arrives with a FIR written as a free-form letter. The template parser that reads
every other FIR finds nothing in it, so a language model reads it (the cheapest method that
can handle the task, as the talk says). The model only *proposes*: its extractions are
checked against the page text, normalised and resolved by the same rules every other mention
goes through, and only then written to the graph, with the model, the check and the
confidence kept as lineage. The fraud signal is refreshed and investigators are alerted.

Each step yields an event, so the demo app can show the steps as they happen.
"""

import json
import re
import shutil
from datetime import datetime, timezone
from typing import Iterator

import pandas as pd
import psycopg
from pydantic import BaseModel, Field

from claims import config, governance, lakehouse, llm, stores
from claims.layers import a_organization as a, c_retrieval as c, e_integration as e, f_signals as f
from claims.normalize import mask_phone, phone_e164

STAGED = config.GROUND_TRUTH.parent / "seventh"
FILE = "complaint_letter_scan.pdf"


class ExtractedFact(BaseModel):
    value: str
    quote: str = Field(description="The exact words in the document the value comes from")


class FIRExtraction(BaseModel):
    claim_reference: ExtractedFact | None = None
    complainant: ExtractedFact | None = None
    vehicle_registration: ExtractedFact | None = None
    repair_shop: ExtractedFact | None = None
    contact_phone: ExtractedFact | None = None


INSTRUCTIONS = ("You extract facts from police complaints (FIRs) about motor accidents in India. Return only what the "
                "document states, with the exact quote each value comes from. Leave a field empty if it is not stated.")


def _event(step: str, status: str, detail: str, **data) -> dict:
    return {"step": step, "status": status, "detail": detail, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **data}


def _regex_fallback(text: str) -> FIRExtraction:
    """Used only when no model and no recording are available, so the demo still runs offline."""
    def fact(rx):
        m = re.search(rx, text, re.S)
        return ExtractedFact(value=" ".join(m.group(1).split()), quote=" ".join(m.group(0).split())) if m else None
    return FIRExtraction(claim_reference=fact(r"claim number (\d{5})"), complainant=fact(r"I am ([A-Z][a-z]+ [A-Z][a-z]+)"),
                         vehicle_registration=fact(r"bearing number ([A-Z]{2} \d{2} [A-Z]{2} \d{4})"),
                         repair_shop=fact(r"towed to (.+?), who"), contact_phone=fact(r"reach me on ([\d ]{10,14})"))


def present() -> bool:
    return (config.UPLOADS / FILE).exists()


def run() -> Iterator[dict]:
    spec = json.loads((STAGED / "seventh.json").read_text())
    claim_id = spec["claim"]["claim_no"]

    # 1 · Ingest: the claim lands in the claims DB, the FIR in uploads; the System of Record picks both up
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        cu, p, cl = spec["customer"], spec["policy"], spec["claim"]
        conn.execute("INSERT INTO crm.customers VALUES (%s,%s,%s,%s,%s,%s,now()) ON CONFLICT DO NOTHING",
                     (cu["cust_id"], cu["full_name"], cu["dob"], cu["phone"], cu["email"], cu["city"]))
        conn.execute("INSERT INTO policy_admin.policies VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (p["pol_no"], p["holder_cust_id"], p["product"], p["veh_reg"], p["make_model"], p["segment"], p["idv"],
                      p["zero_dep"], p["deductible"], p["start_dt"], p["end_dt"], p["status"], p["endorsement"]))
        conn.execute("INSERT INTO policy_admin.policy_audit_log (pol_no, changed_at, change_type, payload) "
                     "SELECT %s, %s, 'ISSUE', %s WHERE NOT EXISTS (SELECT 1 FROM policy_admin.policy_audit_log WHERE pol_no=%s)",
                     (p["pol_no"], p["start_dt"] + " 11:00:00", json.dumps(p), p["pol_no"]))
        conn.execute("INSERT INTO claims_db.claims VALUES (%s,%s,%s,%s,%s,%s,NULL,NULL,'OPEN',%s,%s,%s,'Docs uploaded by customer') "
                     "ON CONFLICT DO NOTHING", (cl["claim_no"], cl["claim_dt"], cl["loss_dt"], cl["pol_no"], cl["cust_id"],
                                                cl["amount"], cl["severity"], cl["loss_type"], cl["veh_reg"]))
        conn.execute("INSERT INTO claims_db.claim_assessments SELECT %s,'SV-131',%s,%s,CURRENT_DATE "
                     "WHERE NOT EXISTS (SELECT 1 FROM claims_db.claim_assessments WHERE claim_no=%s)",
                     (cl["claim_no"], cl["sev_band"], cl["amount"] - 15000, cl["claim_no"]))
    shutil.copy(STAGED / FILE, config.UPLOADS / FILE)
    a.ingest_postgres(); a.ingest_files()
    customers, smap = a.build_customers(); a.build_policy_versions(smap); a.build_claims(customers, smap)
    docs = a.register_documents()
    doc = docs[docs.file_name == FILE].iloc[0].to_dict()
    yield _event("Ingest", "done", f"{claim_id} stored in the System of Record; FIR registered as {doc['doc_id']} "
                 f"(type {doc['doc_type']}, owner {doc['owner']})", claim_id=claim_id, doc_id=doc["doc_id"])

    # 2 · Extract: the template parser fails on a letter, so a model reads it
    pages = c.pdf_pages(config.UPLOADS / FILE)
    text = "\n".join(pages)
    template = c.template_fields("FIR", pages)
    yield _event("Extract", "running", f"Template parser found {len(template)} labelled fields: this FIR is a free-form "
                 "letter. Escalating to a language model.")
    out, how = llm.structured("fir_extraction", INSTRUCTIONS, text, FIRExtraction)
    if out is None:
        out, how = _regex_fallback(text), "regex fallback (no model or recording available)"
    facts = {k: v.model_dump() for k, v in out if v}
    shown = {k: ({**v, "value": mask_phone(phone_e164(v["value"]))} if k == "contact_phone" else v) for k, v in facts.items()}
    yield _event("Extract", "done", f"Model extracted {len(facts)} facts ({how}).", facts=shown, model=config.default_model())

    # 3 · Propose: resolve each extraction with the same rules every other mention goes through
    registry = lakehouse.read("silver.repair_shops")
    proposals = []
    if out.contact_phone:
        e164 = phone_e164(out.contact_phone.value)
        shared = stores.cypher("MATCH (p:Phone {e164:$e})<-[:CONTACT_PHONE]-(c:Claim) RETURN collect(c.id) AS ids", e=e164)[0]["ids"]
        proposals.append({"edge": "CONTACT_PHONE", "entity": f"PHONE:{e164}", "display": mask_phone(e164),
                          "raw": out.contact_phone.value, "quote": out.contact_phone.quote, "rule": "E.164 normalisation",
                          "match": 1.0, "already_on": shared})
    if out.repair_shop:
        sid, rule, conf = e.resolve_shop(out.repair_shop.value, None, registry)
        shared = stores.cypher("MATCH (s:RepairShop {id:$s})<-[:REPAIRED_AT]-(c:Claim) RETURN collect(c.id) AS ids", s=sid)[0]["ids"] if sid else []
        proposals.append({"edge": "REPAIRED_AT", "entity": f"SHOP:{sid}" if sid else None,
                          "display": registry.set_index("shop_id")["name"].get(sid, out.repair_shop.value) if sid else out.repair_shop.value,
                          "raw": out.repair_shop.value, "quote": out.repair_shop.quote, "rule": rule, "match": conf,
                          "already_on": shared})
    yield _event("Propose", "done", " · ".join(f"{p['edge']} → {p['display']} (on {len(p['already_on'])} claims)" for p in proposals),
                 proposals=[{k: v for k, v in p.items() if k != "raw"} for p in proposals])

    # 4 · Validate: the quote must be in the page, the value must normalise, the match must clear the bar
    accepted = []
    flat = " ".join(text.split())
    for p in proposals:
        grounded = " ".join(p["quote"].split()) in flat
        normalised = p["entity"] is not None
        confidence = round((0.6 if grounded else 0.0) + 0.4 * p["match"], 2) if normalised else 0.0
        ok = grounded and normalised and confidence >= 0.85
        p.update(grounded=grounded, confidence=confidence, accepted=ok)
        if ok:
            accepted.append(p)
    yield _event("Validate", "done", "; ".join(f"{p['edge']}: quote {'found' if p['grounded'] else 'NOT found'} in page, "
                                               f"confidence {p['confidence']} → {'accepted' if p['accepted'] else 'rejected'}"
                                               for p in proposals),
                 checks=[{k: p[k] for k in ("edge", "display", "grounded", "match", "confidence", "accepted")} for p in proposals])

    # write: document fields with lineage, retrieval chunk, graph edges, then refresh signals
    fields = lakehouse.read("silver.document_fields")
    new = [{"doc_id": doc["doc_id"], "claim_id": claim_id, "doc_type": "FIR", "field": fld, "value": v.value, "page": 1,
            "method": f"llm:{config.default_model()}", "confidence": 0.9}
           for fld, v in [("contact_phone", out.contact_phone), ("repair_shop", out.repair_shop),
                          ("complainant", out.complainant), ("vehicle_reg", out.vehicle_registration)] if v]
    lakehouse.write("silver.document_fields", pd.concat([fields[fields.doc_id != doc["doc_id"]], pd.DataFrame(new)]))
    pages_df = lakehouse.read("silver.document_pages")
    lakehouse.write("silver.document_pages", pd.concat([pages_df[pages_df.doc_id != doc["doc_id"]], pd.DataFrame(
        [{"doc_id": doc["doc_id"], "claim_id": claim_id, "doc_type": "FIR", "page": i, "text": t} for i, t in enumerate(pages, 1)])]))
    chunks = c.chunk_documents()
    c.index_chunks(chunks[chunks.doc_id == doc["doc_id"]], recreate=False)
    e.collect_mentions(); e.resolve_mentions()
    e.build_graph()
    for p in accepted:   # record that the model proposed and a rule validated each edge
        rel = p["edge"]
        stores.cypher(f"MATCH (c:Claim {{id:$c}})-[r:{rel}]->() SET r.proposed_by=$m, r.validated_by='quote grounding + ' + $rule, "
                      "r.validation_confidence=$conf", c=claim_id, m=config.default_model(), rule=p["rule"], conf=p["confidence"])
    features = f.build_features(); signals = f.build_signals(features); f.link_signals_to_graph(signals)
    score = float(signals[(signals.claim_id == claim_id) & (signals.signal == "fraud_score")].value.iloc[0])
    ring = stores.cypher("MATCH (:Claim {id:$c})-[:CONTACT_PHONE]->(p:Phone)<-[:CONTACT_PHONE]-(o:Claim) RETURN count(DISTINCT o) + 1 AS n", c=claim_id)[0]["n"]
    governance.audit({"run_id": "ai-for-data", "user": "pipeline", "role": "system", "stage": "AI for Data",
                      "tool": "fir_extraction", "args": {"doc_id": doc["doc_id"]}, "backend": f"llm:{config.default_model()}",
                      "proposals": [{k: p[k] for k in ("edge", "confidence", "accepted")} for p in proposals]})
    yield _event("Alert", "done", f"{ring} claims now share the contact phone {mask_phone(phone_e164(out.contact_phone.value)) if out.contact_phone else ''}. "
                 f"fraud_score for {claim_id} is {score}. Investigators alerted; proposals and checks logged in lineage.",
                 ring_size=ring, fraud_score=score, claim_id=claim_id)


def reset() -> dict:
    """Remove the seventh claim everywhere, so the demo can be rehearsed."""
    spec = json.loads((STAGED / "seventh.json").read_text())
    claim_id, pol, cust = spec["claim"]["claim_no"], spec["policy"]["pol_no"], spec["customer"]["cust_id"]
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        conn.execute("DELETE FROM claims_db.claims WHERE claim_no=%s", (claim_id,))
        conn.execute("DELETE FROM claims_db.claim_assessments WHERE claim_no=%s", (claim_id,))
        conn.execute("DELETE FROM policy_admin.policies WHERE pol_no=%s", (pol,))
        conn.execute("DELETE FROM policy_admin.policy_audit_log WHERE pol_no=%s", (pol,))
        conn.execute("DELETE FROM crm.customers WHERE cust_id=%s", (cust,))
    (config.UPLOADS / FILE).unlink(missing_ok=True)
    docs = lakehouse.read("silver.documents")
    gone = docs[docs.file_name == FILE].doc_id.tolist()
    a.ingest_postgres(); a.ingest_files()
    customers, smap = a.build_customers(); a.build_policy_versions(smap); a.build_claims(customers, smap); a.register_documents()
    for t in ("silver.document_fields", "silver.document_pages"):
        df = lakehouse.read(t)
        lakehouse.write(t, df[~df.doc_id.isin(gone)])
    c.chunk_documents()
    from qdrant_client import models
    stores.qdrant().delete("documents", points_selector=models.FilterSelector(filter=models.Filter(
        must=[models.FieldCondition(key="claim_id", match=models.MatchValue(value=claim_id))])))
    e.collect_mentions(); e.resolve_mentions(); e.build_graph()
    signals = f.build_signals(f.build_features()); f.link_signals_to_graph(signals)
    return {"reset": claim_id}
