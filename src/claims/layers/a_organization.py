"""Layer A · Enterprise Data Organization.

Question it answers: are we using the right records for this decision?
Work: standardise, deduplicate, keep history, validate, register documents.

Bronze copies every source unchanged, stamped with its system and ingest time. Silver
gives one claim ID across the claims DB and the regional tracker, one person per human
across CRM duplicates, every policy version rebuilt from the audit log (so terms can be
read as of the accident date), and a register of documents with owner and version.
Rows that fail the gate go to silver.quality_events instead of silently disappearing.
"""

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd
import psycopg
from pypdf import PdfReader

from claims import config, lakehouse
from claims.normalize import claim_id, ddmmyy, phone_e164, surname

PG_TABLES = {
    "bronze.claims_db_claims": ("Claims DB", "SELECT * FROM claims_db.claims"),
    "bronze.claims_db_assessments": ("Claims DB", "SELECT * FROM claims_db.claim_assessments"),
    "bronze.policy_admin_policies": ("Policy Admin", "SELECT * FROM policy_admin.policies"),
    "bronze.policy_audit_log": ("Policy Admin", "SELECT event_id, pol_no, changed_at, change_type, payload::text AS payload FROM policy_admin.policy_audit_log"),
    "bronze.crm_customers": ("CRM", "SELECT * FROM crm.customers"),
}


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(df: pd.DataFrame, system: str) -> pd.DataFrame:
    df = df.copy()
    for col in df.columns:
        if df[col].dtype == object:
            df[col] = df[col].map(lambda v: None if v is None else str(v))
    df["_source_system"] = system
    df["_ingested_at"] = _now()
    return df


def _pg(sql: str) -> pd.DataFrame:
    with psycopg.connect(config.PG_DSN) as conn, conn.cursor() as cur:
        cur.execute(sql)
        cols = [d.name for d in cur.description]
        return pd.DataFrame(cur.fetchall(), columns=cols)


# --------------------------------------------------------------------------- bronze

def ingest_postgres() -> dict[str, int]:
    out = {}
    for name, (system, sql) in PG_TABLES.items():
        df = _pg(sql)
        for col in df.columns:   # Postgres numeric arrives as Decimal; the lakehouse stores doubles
            if df[col].map(lambda v: isinstance(v, Decimal)).any():
                df[col] = df[col].map(lambda v: None if v is None else float(v))
        out[name] = lakehouse.write(name, _stamp(df, system))
    return out


def ingest_files() -> dict[str, int]:
    sheet = pd.read_csv(config.SOURCES / "regional_claims_tracker.csv", dtype=str).fillna("")
    registry = pd.read_csv(config.SOURCES / "garage_registry.csv", dtype=str)
    inventory = []
    for path in sorted(config.UPLOADS.glob("*.pdf")):
        blob = path.read_bytes()
        inventory.append({"file_name": path.name, "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob),
                          "modified_at": datetime.fromtimestamp(path.stat().st_mtime)})
    return {
        "bronze.claims_sheet": lakehouse.write("bronze.claims_sheet", _stamp(sheet, "Regional Tracker")),
        "bronze.garage_registry": lakehouse.write("bronze.garage_registry", _stamp(registry, "Garage Registry")),
        "bronze.document_inventory": lakehouse.write("bronze.document_inventory", _stamp(pd.DataFrame(inventory), "Uploads")),
    }


# --------------------------------------------------------------------------- silver

def build_customers() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Resolve CRM duplicates: same phone and date of birth is the same person."""
    crm = lakehouse.read("bronze.crm_customers")
    crm["phone_e164"] = crm["phone"].map(phone_e164)
    crm["dob"] = pd.to_datetime(crm["dob"]).dt.date
    groups = crm.groupby(["phone_e164", "dob"], dropna=False)
    people, links = [], []
    for (phone, dob), g in groups:
        g = g.sort_values("created_at")
        anchor = int(g["cust_id"].min())
        key = "PER-" + hashlib.sha1(str(anchor).encode()).hexdigest()[:6].upper()
        best = max(g["full_name"], key=len)
        same_surname = len({surname(n) for n in g["full_name"]}) == 1
        conf = 1.0 if len(g) == 1 else (0.98 if same_surname else 0.9)
        rule = "single record" if len(g) == 1 else "phone_e164 + dob" + (" + surname" if same_surname else "")
        people.append({"customer_key": key, "full_name": best, "dob": dob, "phone_e164": phone,
                       "email": g.iloc[0]["email"], "city": g.iloc[0]["city"],
                       "source_ids": ",".join(f"crm:{int(i)}" for i in g["cust_id"]),
                       "resolution_rule": rule, "resolution_confidence": conf})
        for _, r in g.iterrows():
            links.append({"source_system": "CRM", "source_id": str(int(r["cust_id"])), "customer_key": key,
                          "name_in_source": r["full_name"], "rule": rule, "confidence": conf})
    customers, source_map = pd.DataFrame(people), pd.DataFrame(links)
    lakehouse.write("silver.customers", customers)
    lakehouse.write("silver.customer_source_map", source_map)
    return customers, source_map


def build_policy_versions(source_map: pd.DataFrame | None = None) -> pd.DataFrame:
    """Rebuild every version of every policy from the audit log's snapshots."""
    source_map = source_map if source_map is not None else lakehouse.read("silver.customer_source_map")
    key_of = dict(zip(source_map["source_id"], source_map["customer_key"]))
    log = lakehouse.read("bronze.policy_audit_log").sort_values(["pol_no", "changed_at"])
    rows = []
    for pol, g in log.groupby("pol_no"):
        events = g.to_dict("records")
        for i, e in enumerate(events):
            snap = json.loads(e["payload"])
            rows.append({
                "policy_id": pol, "version": i + 1, "valid_from": e["changed_at"],
                "valid_to": events[i + 1]["changed_at"] if i + 1 < len(events) else pd.NaT,
                "change_type": e["change_type"], "endorsement_id": snap.get("endorsement"),
                "holder_customer_key": key_of.get(str(snap["holder_cust_id"])), "product": snap["product"],
                "vehicle_reg": snap["veh_reg"], "make_model": snap["make_model"], "segment": snap["segment"],
                "idv": float(snap["idv"]), "zero_dep": bool(snap["zero_dep"]), "deductible": float(snap["deductible"]),
                "term_start": pd.to_datetime(snap["start_dt"]).date(), "term_end": pd.to_datetime(snap["end_dt"]).date(),
                "status": snap["status"],
            })
    df = pd.DataFrame(rows)
    df["valid_to"] = pd.to_datetime(df["valid_to"])
    lakehouse.write("silver.policy_versions", df)
    return df


def build_claims(customers: pd.DataFrame | None = None, source_map: pd.DataFrame | None = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """One row per claim across the claims DB and the regional tracker; the gate holds bad rows."""
    customers = customers if customers is not None else lakehouse.read("silver.customers")
    source_map = source_map if source_map is not None else lakehouse.read("silver.customer_source_map")
    key_of = dict(zip(source_map["source_id"], source_map["customer_key"]))
    key_by_phone = dict(zip(customers["phone_e164"], customers["customer_key"]))

    db = lakehouse.read("bronze.claims_db_claims")
    assess = lakehouse.read("bronze.claims_db_assessments").drop_duplicates("claim_no", keep="last")
    db = db.merge(assess[["claim_no", "sev_band", "assessed_amt"]], on="claim_no", how="left")
    rows, events = {}, []
    for r in db.to_dict("records"):
        cid = claim_id(r["claim_no"])
        rows[cid] = {
            "claim_id": cid, "filed_date": ddmmyy(r["claim_dt"]), "loss_date": ddmmyy(r["loss_dt"]),
            "policy_id": r["pol_no"], "customer_key": key_of.get(str(int(r["cust_id"]))),
            "requested_amount": float(r["amount"]),
            "approved_amount": float(r["amt_appr"]) if pd.notna(r["amt_appr"]) else None,
            "paid_amount": float(r["paid_amt"]) if pd.notna(r["paid_amt"]) else None,
            "assessed_amount": float(r["assessed_amt"]) if pd.notna(r["assessed_amt"]) else None,
            "status": r["status"], "severity_band": r["sev_band"], "severity_code_legacy": int(r["severity"]),
            "loss_type": r["loss_type"], "vehicle_reg": r["veh_reg"], "sources": "claims_db",
        }
    sheet = lakehouse.read("bronze.claims_sheet")
    for r in sheet.to_dict("records"):
        cid = claim_id(r["Claim Ref"])
        if cid in rows:
            rows[cid]["sources"] = "claims_db,regional_tracker"
            continue
        rows[cid] = {
            "claim_id": cid, "filed_date": ddmmyy(r["Date Reported"]), "loss_date": ddmmyy(r["Loss Date"]),
            "policy_id": r["Policy No"] or None, "customer_key": key_by_phone.get(phone_e164(r["Contact"])),
            "requested_amount": float(r["Est. Amount (INR)"]), "approved_amount": None, "paid_amount": None,
            "assessed_amount": None, "status": "OPEN", "severity_band": None, "severity_code_legacy": None,
            "loss_type": "COLL", "vehicle_reg": None, "sources": "regional_tracker",
        }
    claims = pd.DataFrame(rows.values())

    # the silver gate: a claim without a policy cannot be assessed; hold it and say why
    held = claims[claims["policy_id"].isna()]
    for r in held.to_dict("records"):
        events.append({"dataset": "silver.claims", "record_key": r["claim_id"], "rule": "policy_id is required",
                       "detail": f"claim from {r['sources']} has no policy number", "status": "held",
                       "detected_at": _now()})
    claims = claims[claims["policy_id"].notna()].copy()

    # vehicle for tracker-only claims comes from the policy
    pv = lakehouse.read("silver.policy_versions").sort_values("version").drop_duplicates("policy_id", keep="last")
    reg_of = dict(zip(pv["policy_id"], pv["vehicle_reg"]))
    claims["vehicle_reg"] = claims["vehicle_reg"].fillna(claims["policy_id"].map(reg_of))
    claims["severity_code_legacy"] = claims["severity_code_legacy"].astype("Int64")

    lakehouse.write("silver.claims", claims)
    q = pd.DataFrame(events) if events else pd.DataFrame(columns=["dataset", "record_key", "rule", "detail", "status", "detected_at"])
    lakehouse.write("silver.quality_events", q)
    return claims, q


def _doc_type(text: str) -> str:
    t = text.upper()
    if "FIRST INFORMATION REPORT" in t or "STATION HOUSE OFFICER" in t:
        return "FIR"
    if "SURVEY REPORT" in t:
        return "SURVEY"
    if "TAX INVOICE" in t or "GSTIN" in t:
        return "INVOICE"
    return "UNKNOWN"


def register_documents(paths=None) -> pd.DataFrame:
    """Give every upload a claim, a type, a version and an owner, read from the content."""
    inventory = lakehouse.read("bronze.document_inventory")
    rows = []
    for r in inventory.to_dict("records"):
        path = config.UPLOADS / r["file_name"]
        reader = PdfReader(str(path))
        text = "\n".join(p.extract_text() or "" for p in reader.pages)
        rows.append({"doc_id": "DOC-" + r["sha256"][:8].upper(), "file_name": r["file_name"], "doc_type": _doc_type(text),
                     "claim_id": claim_id(text), "sha256": r["sha256"], "pages": len(reader.pages),
                     "revision": "-R" in text, "owner": "Claims Operations", "registered_at": _now()})
    docs = pd.DataFrame(rows)
    docs = docs.sort_values(["claim_id", "doc_type", "revision", "file_name"])
    docs["version"] = docs.groupby(["claim_id", "doc_type"]).cumcount() + 1
    latest = docs.groupby(["claim_id", "doc_type"])["version"].transform("max")
    docs["is_latest"] = docs["version"] == latest
    docs["supersedes"] = docs.apply(
        lambda r: docs[(docs.claim_id == r.claim_id) & (docs.doc_type == r.doc_type) & (docs.version == r.version - 1)]["doc_id"].max()
        if r.version > 1 else None, axis=1)
    docs = docs.drop(columns=["revision"])
    lakehouse.write("silver.documents", docs)
    return docs


def build_repair_shops() -> pd.DataFrame:
    reg = lakehouse.read("bronze.garage_registry")
    shops = pd.DataFrame({"shop_id": reg["gstin"], "name": reg["registered_name"], "address": reg["address"],
                          "city": reg["city"], "phone_e164": reg["phone"].map(phone_e164), "network_status": reg["network_status"]})
    lakehouse.write("silver.repair_shops", shops)
    return shops
