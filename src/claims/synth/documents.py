"""Render the claim documents as PDFs: FIRs, repair invoices and one surveyor report.

The FIR puts the incident on page 1 and the complainant's statement on page 2, so the
statement that names the repair shop and the contact phone is cited as "FIR page 2".
File names are deliberately messy (scan_0042.pdf, IMG_2210_invoice.pdf): the uploads
folder has no owner and no version, and only the content says which claim a file is for.
"""

import random
from pathlib import Path

from fpdf import FPDF

from claims.synth.world import Claim, Customer


def _pdf() -> FPDF:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    return pdf


def _heading(pdf: FPDF, text: str, sub: str = ""):
    pdf.set_font("Helvetica", "B", 14)
    pdf.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
    if sub:
        pdf.set_font("Helvetica", "", 9)
        pdf.cell(0, 5, sub, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(4)


def _field(pdf: FPDF, label: str, value: str):
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(52, 6.5, label)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6.5, value, new_x="LMARGIN", new_y="NEXT")


def _para(pdf: FPDF, text: str):
    pdf.set_font("Helvetica", "", 10.5)
    pdf.multi_cell(0, 6, text, new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)


def _rs(n: int) -> str:
    return f"Rs. {n:,}"


STATIONS = ["Hadapsar", "Wanowrie", "Kothrud", "Chatushrungi", "Wakad", "Vimantal", "Kondhwa", "Andheri (E)"]
PLACES = ["Magarpatta junction", "Solapur Road near Fatima Nagar", "Baner-Pashan link road", "NIBM Road",
          "Mumbai-Bangalore highway service road", "Nagar Road near Phoenix Mall", "Sinhagad Road flyover"]


def render_fir(path: Path, claim: Claim, cust: Customer, shop_written: str, rng: random.Random):
    pdf = _pdf()
    station = "Hadapsar" if claim.shop_gstin.startswith("27AAFCF") else rng.choice(STATIONS)
    place = rng.choice(PLACES)
    fir_no = f"{rng.randint(300, 1900)}/{claim.loss.year}"
    pdf.add_page()
    _heading(pdf, "FIRST INFORMATION REPORT", "Illustrative document generated for a demo. Not a real record.")
    _field(pdf, "Police Station:", f"{station}, Pune City")
    _field(pdf, "FIR No.:", fir_no)
    _field(pdf, "Date of report:", claim.loss.strftime("%d-%m-%Y"))
    _field(pdf, "Date of occurrence:", claim.loss.strftime("%d-%m-%Y") + f", approx. {rng.randint(7, 22)}:{rng.choice(['05', '20', '40', '55'])} hrs")
    _field(pdf, "Place of occurrence:", place)
    _field(pdf, "Complainant:", cust.full_name)
    _field(pdf, "Vehicle Reg. No.:", claim.veh_reg)
    _field(pdf, "Vehicle make/model:", claim.make_model)
    _field(pdf, "Insurance claim ref:", claim.claim_no)
    pdf.ln(3)
    _para(pdf, "Nature of offence: road accident involving damage to property. No injuries reported. "
               "Sections as applicable under the Motor Vehicles Act.")

    pdf.add_page()
    _heading(pdf, "STATEMENT OF COMPLAINANT", f"FIR No. {fir_no} - continued")
    other = rng.choice(["a speeding tempo", "a two-wheeler that cut across", "an unidentified truck", "a car reversing out of a lane"])
    impact = rng.choice(["front bumper, bonnet and radiator", "front-left fender, headlamp and suspension",
                         "front grille, airbag module and bonnet", "rear door, quarter panel and tail lamp"])
    _para(pdf, f"I, {cust.full_name}, state that on the above date my vehicle {claim.veh_reg} was hit by {other} "
               f"near {place}. The {impact} were damaged. The other vehicle did not stop.")
    _para(pdf, f"The vehicle was not driveable and was later taken to {shop_written} for repair. "
               "I have informed my insurer and will submit the repair estimate and invoice.")
    _field(pdf, "Contact No.:", claim.fir_phone)
    _para(pdf, "The above statement has been read over to me and is correct.")
    pdf.output(str(path))


def render_invoice(path: Path, claim: Claim, cust: Customer, shop: tuple, shop_written: str, amount: int,
                   rng: random.Random, include_gstin: bool = True, revision: str = ""):
    gstin, _, address, city, phone = shop
    pdf = _pdf()
    pdf.add_page()
    _heading(pdf, shop_written.upper(), f"{address}, {city}  |  Tel {phone[:3]} {phone[3:5]} {phone[5:]}")
    if include_gstin:
        _field(pdf, "GSTIN:", gstin)
    _field(pdf, "Tax invoice no.:", f"INV/{claim.filed.strftime('%y%m')}/{rng.randint(100, 999)}{revision}")
    _field(pdf, "Invoice date:", claim.filed.strftime("%d-%m-%Y"))
    _field(pdf, "Customer:", cust.full_name)
    _field(pdf, "Vehicle:", f"{claim.veh_reg}  ({claim.make_model})")
    _field(pdf, "Insurance claim ref:", claim.claim_no)
    pdf.ln(3)
    parts = int(amount * 0.68 / 100) * 100
    labour = int(amount * 0.22 / 100) * 100
    paint = amount - parts - labour
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(120, 7, "Description", border="B")
    pdf.cell(0, 7, "Amount", border="B", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "", 10)
    for desc, amt in [("Parts (metal, plastic, glass)", parts), ("Labour", labour), ("Paint and consumables", paint)]:
        pdf.cell(120, 7, desc)
        pdf.cell(0, 7, _rs(amt), align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(120, 8, "Total (incl. GST)", border="T")
    pdf.cell(0, 8, _rs(amount), border="T", align="R", new_x="LMARGIN", new_y="NEXT")
    pdf.output(str(path))


def render_survey(path: Path, claim: Claim, cust: Customer, assessed: int):
    pdf = _pdf()
    pdf.add_page()
    _heading(pdf, "MOTOR SURVEY REPORT", "Illustrative document generated for a demo. Not a real record.")
    _field(pdf, "Surveyor:", "S. Kulkarni (Licence SLA-44817)")
    _field(pdf, "Insurance claim ref:", claim.claim_no)
    _field(pdf, "Vehicle:", f"{claim.veh_reg}  ({claim.make_model})")
    _field(pdf, "Inspected on:", claim.filed.strftime("%d-%m-%Y"))
    _field(pdf, "Severity band:", f"{claim.sev_band} (major repair: structural damage, airbag deployed)")
    _field(pdf, "Assessed loss:", _rs(assessed))
    pdf.ln(3)
    _para(pdf, "Observations: frontal impact consistent with the narrative in the FIR. Radiator support, bonnet "
               "and front cross member require replacement. Vehicle inspected at the repairer's premises.")
    pdf.output(str(path))


def render_freeform_fir(path: Path, claim: Claim, cust: Customer):
    """The seventh claim's FIR: a typed letter with no field labels, so the template parser
    finds nothing and the document needs a language model to read it."""
    pdf = _pdf()
    pdf.add_page()
    _para(pdf, "To,\nThe Station House Officer,\nHadapsar Police Station, Pune")
    _para(pdf, f"Date: {claim.loss.strftime('%d/%m/%Y')}")
    _para(pdf, "Subject: Complaint regarding accident and damage to my car")
    _para(pdf, "Respected Sir,")
    _para(pdf, f"I am {cust.full_name}, resident of Magarpatta City. Yesterday evening my Kia Seltos bearing number "
               f"MH 12 KP 7781 was dashed from behind by a dumper near the Magarpatta junction and pushed into the "
               f"divider. The front portion and the airbags are badly damaged and the car could not be driven. "
               f"With the help of people there I got it towed to {claim.shop_written}, who are doing the repair.")
    _para(pdf, f"My insurance company has given claim number {claim.claim_no[4:]} for this. Kindly register my "
               f"complaint. For any further information you can reach me on {claim.fir_phone} any time.")
    _para(pdf, f"Thanking you,\n{cust.full_name}")
    pdf.output(str(path))
