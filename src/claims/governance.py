"""Layer D · Governance & Control (request time): masking, the action gate and the audit trail.

Enforcement is distributed, as the paper says: the SQL session only ever holds masked
tables, retrieved passages are redacted before they reach the model, graph results carry
masked phone values, and actions go through a gate that knows which need a person.
Every call is written to the audit log with the role, what was touched and what was masked.
"""

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

import duckdb
import pandas as pd
import yaml

from claims import config, lakehouse
from claims.normalize import PHONE, mask_phone, phone_e164


@dataclass
class Principal:
    user: str = "priya.n"
    role: str = "claims_officer"
    assigned_claim: str = config.CASE_CLAIM


@dataclass
class Redactor:
    """The assigned claimant keeps their name; everyone else gets a stable pseudonym from their person key."""
    principal: Principal
    assigned_key: str | None = None
    name_to_key: list[tuple[str, str]] = field(default_factory=list)
    masked_count: int = 0

    @classmethod
    def for_principal(cls, principal: Principal) -> "Redactor":
        r = cls(principal)
        claims = lakehouse.read("silver.claims")
        row = claims[claims.claim_id == principal.assigned_claim]
        r.assigned_key = row.iloc[0]["customer_key"] if len(row) else None
        names = lakehouse.read("silver.customer_source_map")[["name_in_source", "customer_key"]]
        best = lakehouse.read("silver.customers")[["full_name", "customer_key"]].rename(columns={"full_name": "name_in_source"})
        pairs = pd.concat([names, best]).drop_duplicates()
        r.name_to_key = sorted(pairs.itertuples(index=False, name=None), key=lambda p: -len(p[0]))
        return r

    @property
    def sees_pii(self) -> bool:
        return self.principal.role == "investigator"

    def label(self, key: str | None) -> str:
        return f"Person {key.removeprefix('PER-')}" if key else "Person ?"

    def name(self, full_name: str | None, key: str | None) -> str | None:
        if full_name is None or self.sees_pii or key == self.assigned_key:
            return full_name
        self.masked_count += 1
        return self.label(key)

    def phone(self, value: str | None) -> str | None:
        if value is None or self.sees_pii:
            return value
        self.masked_count += 1
        return mask_phone(phone_e164(value) or value)

    def text(self, text: str | None) -> str | None:
        if text is None or self.sees_pii:
            return text
        out = PHONE.sub(lambda m: self.phone(m.group(0)) or "", text)
        for name, key in self.name_to_key:
            if key != self.assigned_key and name in out:
                self.masked_count += 1
                out = out.replace(name, self.label(key))
        return out


def classified_columns() -> dict[str, dict[str, str]]:
    cls = lakehouse.read("meta.classifications")
    cols = cls[~cls.asset.str.startswith("chunk:")]
    out: dict[str, dict[str, str]] = {}
    for asset, c in zip(cols.asset, cols.classification):
        ns, table, col = asset.split(".")
        out.setdefault(f"{ns}.{table}", {})[col] = c
    return out


def governed_session(redactor: Redactor, names: list[str]) -> duckdb.DuckDBPyConnection:
    """A DuckDB session over the given tables in which classified columns are already masked."""
    con = lakehouse.duck(names)
    if redactor.sees_pii:
        return con
    con.create_function("_mask_phone", lambda v: redactor.phone(v), ["VARCHAR"], "VARCHAR", null_handling="special")
    con.create_function("_mask_name", lambda n, k: redactor.name(n, k), ["VARCHAR", "VARCHAR"], "VARCHAR", null_handling="special")
    con.create_function("_redact", lambda t: redactor.text(t), ["VARCHAR"], "VARCHAR", null_handling="special")
    for table, cols in classified_columns().items():
        if table not in names:
            continue
        replace = []
        present = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
        for col, cls in cols.items():
            if col not in present:
                continue
            if table == "silver.document_chunks" and col == "text":
                replace.append("_redact(text) AS text")
            elif cls == "PII.Phone":
                replace.append(f"_mask_phone({col}) AS {col}")
            elif cls == "PII.Name" and "customer_key" in present:
                replace.append(f"_mask_name({col}, customer_key) AS {col}")
            else:
                replace.append(f"NULL::VARCHAR AS {col}")
        if replace:
            con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT * REPLACE ({', '.join(replace)}) FROM {table}")
    return con


def gate_action(action: str, principal: Principal) -> dict:
    rules = yaml.safe_load((config.CATALOG_AS_CODE / "policies.yaml").read_text())["actions"]
    rule = rules.get(action)
    if rule is None:
        return {"allowed": False, "status": "rejected", "reason": f"unknown action '{action}'"}
    if principal.role not in rule["allowed_for"]:
        return {"allowed": False, "status": "rejected", "reason": f"role {principal.role} may not {action}"}
    if rule["requires_approval"]:
        return {"allowed": True, "status": "pending_approval", "approver_role": rule["approver_role"],
                "reason": f"{action} needs approval by {rule['approver_role']}; nothing is paid until a person approves"}
    return {"allowed": True, "status": "done", "reason": f"{action} recorded"}


_audit_lock = threading.Lock()
TABLE_REF = re.compile(r"\b(bronze|silver|gold|meta|claims_db|policy_admin|crm)\.(\w+)", re.I)


def touched(sql: str) -> list[str]:
    return sorted({f"{a.lower()}.{b.lower()}" for a, b in TABLE_REF.findall(sql)})


def audit(event: dict):
    config.AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    event = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **event}
    with _audit_lock, config.AUDIT_LOG.open("a") as f:
        f.write(json.dumps(event, default=str, ensure_ascii=False) + "\n")
