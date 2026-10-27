"""Layer C · Retrieval-Ready Knowledge.

Question it answers: can we retrieve the supporting evidence?
Work: extract, chunk by section, embed, index with source.

Template-aware parsing pulls labelled fields from the recurring formats (FIR, invoice,
survey). Chunks follow the document's own sections and carry a context header, so a
passage found by meaning can be cited as document, page and section. Personal data is
indexed as-is here; Layer D decides who may see it.
"""

import hashlib
import re

import pandas as pd
from pypdf import PdfReader
from qdrant_client import models

from claims import config, lakehouse, stores

FIR_FIELDS = {
    "complainant": re.compile(r"Complainant:\s*(.+)"),
    "vehicle_reg": re.compile(r"Vehicle Reg\. No\.:\s*(\S+)"),
    "place": re.compile(r"Place of occurrence:\s*(.+)"),
    "contact_phone": re.compile(r"Contact No\.:\s*(.+)"),
    "repair_shop": re.compile(r"taken to (.+?) for\s+repair", re.S),
}
INVOICE_FIELDS = {
    "gstin": re.compile(r"GSTIN:\s*(\S+)"),
    "customer": re.compile(r"Customer:\s*(.+)"),
    "total": re.compile(r"Total \(incl\. GST\)\s*Rs\.\s*([\d,]+)"),
}
SURVEY_FIELDS = {
    "severity_band": re.compile(r"Severity band:\s*(B\d)"),
    "assessed_amount": re.compile(r"Assessed loss:\s*Rs\.\s*([\d,]+)"),
}
SECTION = {("FIR", 1): "Incident details", ("FIR", 2): "Statement of complainant",
           ("INVOICE", 1): "Repair invoice", ("SURVEY", 1): "Survey findings"}


def pdf_pages(path) -> list[str]:
    return [p.extract_text() or "" for p in PdfReader(str(path)).pages]


def template_fields(doc_type: str, pages: list[str]) -> list[dict]:
    """Labelled fields from a known template. Returns nothing for documents that don't follow one."""
    out = []
    specs = {"FIR": FIR_FIELDS, "INVOICE": INVOICE_FIELDS, "SURVEY": SURVEY_FIELDS}.get(doc_type, {})
    for page_no, text in enumerate(pages, start=1):
        for field, rx in specs.items():
            m = rx.search(text)
            if m:
                out.append({"field": field, "value": " ".join(m.group(1).split()), "page": page_no,
                            "method": "template", "confidence": 0.99})
        if doc_type == "INVOICE" and page_no == 1:
            first = text.strip().splitlines()[0].strip()
            out.append({"field": "repair_shop", "value": first, "page": 1, "method": "template", "confidence": 0.95})
    return out


def parse_documents() -> tuple[pd.DataFrame, pd.DataFrame]:
    docs = lakehouse.read("silver.documents")
    pages, fields = [], []
    for d in docs[docs.is_latest].to_dict("records"):
        texts = pdf_pages(config.UPLOADS / d["file_name"])
        for i, t in enumerate(texts, start=1):
            pages.append({"doc_id": d["doc_id"], "claim_id": d["claim_id"], "doc_type": d["doc_type"], "page": i, "text": t})
        for f in template_fields(d["doc_type"], texts):
            fields.append({"doc_id": d["doc_id"], "claim_id": d["claim_id"], "doc_type": d["doc_type"], **f})
    pages_df, fields_df = pd.DataFrame(pages), pd.DataFrame(fields)
    lakehouse.write("silver.document_pages", pages_df)
    lakehouse.write("silver.document_fields", fields_df)
    return pages_df, fields_df


def chunk_page(doc: dict, page: int, text: str) -> list[dict]:
    """One chunk per section, prefixed with where it came from so the passage stands alone."""
    body = re.sub(r"Illustrative document generated for a demo\. Not a real record\.\n?", "", text).strip()
    section = SECTION.get((doc["doc_type"], page), "Body")
    header = f"[{doc['doc_type']} · {doc['claim_id']} · {doc['file_name']} · page {page} · {section}]"
    return [{"chunk_id": f"{doc['doc_id']}-p{page}", "doc_id": doc["doc_id"], "claim_id": doc["claim_id"],
             "doc_type": doc["doc_type"], "file_name": doc["file_name"], "page": page, "section": section,
             "version": int(doc["version"]), "text": f"{header}\n{body}"}]


def chunk_documents() -> pd.DataFrame:
    docs = {d["doc_id"]: d for d in lakehouse.read("silver.documents").to_dict("records")}
    pages = lakehouse.read("silver.document_pages")
    chunks = [c for p in pages.to_dict("records") for c in chunk_page(docs[p["doc_id"]], p["page"], p["text"])]
    df = pd.DataFrame(chunks)
    lakehouse.write("silver.document_chunks", df)
    return df


def index_chunks(chunks: pd.DataFrame, recreate: bool = True) -> int:
    if recreate:
        stores.recreate_collection("documents")
    rows = chunks.to_dict("records")
    vectors = stores.embed([r["text"] for r in rows])
    points = []
    for r, v in zip(rows, vectors):
        payload = {k: r[k] for k in ("chunk_id", "doc_id", "claim_id", "doc_type", "file_name", "page", "section", "version", "text")}
        payload["governed"] = False          # Layer D adds classifications to every payload
        points.append(models.PointStruct(id=_stable_id(r["chunk_id"]), vector=v, payload=payload))
    stores.qdrant().upsert("documents", points=points)
    return len(points)


def _stable_id(chunk_id: str) -> int:
    return int(hashlib.sha1(chunk_id.encode()).hexdigest()[:15], 16)
