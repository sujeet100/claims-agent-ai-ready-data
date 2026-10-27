"""Standardisation rules shared by every layer: phones, claim references, names, masks."""

import re
from datetime import date, datetime

CLAIM_REF = re.compile(r"\bCLM[\s\-]?(\d{5})\b|\bclaim (?:number|no\.?|ref(?:erence)?)\s*[:#]?\s*(\d{5})\b", re.I)
PHONE = re.compile(r"(?:\+91[\s\-]?|0)?[6-9]\d{4}[\s\-]?\d{5}\b")


def phone_e164(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return "+91" + digits
    if len(digits) == 10:                      # landline with STD code, e.g. 2026871100
        return "+91" + digits
    return None


def mask_phone(e164: str | None) -> str | None:
    """+919822017643 -> '+91 98••• 43', the form the deck shows."""
    if not e164:
        return e164
    d = e164[3:]
    return f"+91 {d[:2]}••• {d[-2:]}"


def claim_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    m = CLAIM_REF.search(str(raw)) or re.search(r"\b(\d{5})\b", str(raw))
    if not m:
        return None
    num = next(g for g in m.groups() if g)
    return f"CLM-{num}"


def ddmmyy(raw: str | None) -> date | None:
    if not raw:
        return None
    for fmt in ("%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(raw.strip(), fmt).date()
        except ValueError:
            continue
    return None


_SHOP_NOISE = re.compile(r"\b(pvt|private|ltd|limited|llp|co|company|the)\b\.?", re.I)


def shop_key(name: str) -> str:
    """Normalise a shop name for fuzzy matching: 'FIXIT MOTORS PVT. LTD.' -> 'fixit motors'."""
    s = name.lower().replace("-", "").replace("&", " and ")
    s = s.split(",")[0]
    s = re.sub(r"\bnear\b.*$", "", s)
    s = _SHOP_NOISE.sub(" ", s)
    s = re.sub(r"\bmotor works\b", "motors", s)
    s = re.sub(r"\bmotor\b", "motors", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(name: str) -> str:
    return name.replace(".", " ").split()[-1].lower() if name else ""
