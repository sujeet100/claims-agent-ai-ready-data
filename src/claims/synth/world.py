"""The synthetic insurer, as its source systems hold it today.

Everything the talk says about claim CLM-88421 is planted here on purpose, and every
other record is generated around it so the planted facts have to be *found*:

- the claims DB writes dates as dd/mm/yy and calls the claim "CLM-88421"; the regional
  spreadsheet writes "CLM 88421" and ISO dates; two ring claims exist only in the sheet
- the policy system keeps only the current view; a zero-depreciation add-on was endorsed
  the day *after* the accident, and that history lives only in the audit log
- the CRM holds the claimant twice (4471 and 9032) with the phone in two formats
- "severity" is a 1-5 code in the claims DB, Low/Med/High in the sheet and a B1-B4 band
  (B1 = total loss, B2 = major repair) in the surveyor's assessment
- the repair shop and a shared contact phone appear only inside the FIR and invoice PDFs,
  in four spellings and four phone formats, across six claims in thirty days
"""

import random
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta

from faker import Faker

SEED = 7

RING_PHONE = "+919822017643"            # shown masked as +91 98••• 43
FIXIT_GSTIN = "27AAFCF4821K1Z3"

# (claim, claimant, filed, accident, requested ₹, shop spelling on invoice, phone as written in FIR, in DB?)
RING = [
    ("CLM-88421", "Arjun Malhotra", date(2026, 10, 14), date(2026, 10, 12), 420000, "FixIt Motors", "+91 98220 17643", True),
    ("CLM-77214", "Sneha Patil", date(2026, 10, 6), date(2026, 10, 4), 385000, "Fixit Motor Works", "098220-17643", True),
    ("CLM-61090", "Vikram Joshi", date(2026, 9, 29), date(2026, 9, 27), 405000, "FIXIT MOTORS PVT. LTD.", "9822017643", True),
    ("CLM-55831", "Kavita Deshmukh", date(2026, 9, 24), date(2026, 9, 22), 395000, "Fix-It Motors, Hadapsar", "+91-9822017643", True),
    ("CLM-70318", "Rahul Kale", date(2026, 9, 26), date(2026, 9, 24), 410000, "FixIt Motors", "+91 98220 17643", False),
    ("CLM-64457", "Meera Iyer", date(2026, 9, 22), date(2026, 9, 20), 390000, "Fixit Motors", "98220 17643", False),
]
# Pseudonyms the governed layers show to a claims officer. They match the deck's labels.
PSEUDONYMS = {"Arjun Malhotra": "Claimant A", "Sneha Patil": "Claimant C", "Vikram Joshi": "Claimant D",
              "Kavita Deshmukh": "Claimant E", "Rahul Kale": "Claimant F", "Meera Iyer": "Claimant G",
              "Nikhil Shinde": "Claimant H"}

SEVENTH = ("CLM-91207", "Nikhil Shinde", date(2026, 10, 21), date(2026, 10, 19), 415000)

SHOPS = [
    (FIXIT_GSTIN, "FixIt Motors", "Plot 14, Hadapsar Industrial Estate", "Pune", "+912026871100"),
    ("27AABCA1102M1Z8", "Autocraft Bodyworks", "Baner Road", "Pune", "+912025671234"),
    ("27AACCS5521Q1Z2", "Sai Service Wakad", "Wakad Chowk", "Pune", "+912027290011"),
    ("27AADCK7781R1Z5", "Kothrud Car Care", "Paud Road, Kothrud", "Pune", "+912025438890"),
    ("27AAECM3345P1Z9", "Metro Motors Viman Nagar", "Viman Nagar", "Pune", "+912026634455"),
    ("27AAFCP9012L1Z1", "Pimpri Auto Hub", "Old Mumbai Highway, Pimpri", "Pune", "+912027471122"),
    ("27AAGCR4420N1Z4", "Royal Garage Aundh", "ITI Road, Aundh", "Pune", "+912025880099"),
    ("27AAHCS6618K1Z7", "Speedway Collision Centre", "Nagar Road, Kharadi", "Pune", "+912026701133"),
    ("27AAJCT2290J1Z6", "Trident Auto Works", "Sinhagad Road", "Pune", "+912024352211"),
    ("27AAKCV8873H1Z0", "Velocity Car Clinic", "Bavdhan", "Pune", "+912022951177"),
    ("27AALCW1150G1Z3", "Westend Motors Andheri", "Andheri East", "Mumbai", "+912228371100"),
    ("27AAMCX5567F1Z2", "Xpress Body Shop Thane", "Ghodbunder Road", "Mumbai", "+912225861144"),
]

SEGMENT_MODELS = {
    "SUV": ["Hyundai Creta", "Kia Seltos", "Mahindra XUV700", "Tata Harrier", "MG Hector"],
    "SEDAN": ["Honda City", "Hyundai Verna", "Maruti Ciaz", "Skoda Slavia"],
    "HATCH": ["Maruti Swift", "Hyundai i20", "Tata Altroz", "Maruti Baleno"],
}


@dataclass
class Customer:
    cust_id: int
    full_name: str
    dob: date
    phone: str          # as typed into the CRM, in whatever format that office used
    email: str
    city: str
    created_at: datetime
    person: str         # ground-truth person key (not in the source system)


@dataclass
class PolicyEvent:
    pol_no: str
    changed_at: datetime
    change_type: str
    snapshot: dict


@dataclass
class Claim:
    claim_no: str
    filed: date
    loss: date
    pol_no: str | None
    cust_id: int
    requested: int
    approved: int | None
    paid: int | None
    status: str
    severity: int       # claims DB: 1-5, 5 worst
    sev_band: str       # surveyor: B1 total loss .. B4 minor
    loss_type: str
    veh_reg: str
    shop_gstin: str
    shop_written: str   # how the shop's name appears on the invoice
    fir_phone: str      # contact phone as written in the FIR
    in_db: bool = True
    in_sheet: bool = False
    has_docs: bool = False
    segment: str = "SEDAN"
    make_model: str = ""


@dataclass
class World:
    customers: list[Customer] = field(default_factory=list)
    policies: dict[str, dict] = field(default_factory=dict)   # current view, as the policy system keeps it
    policy_events: list[PolicyEvent] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)
    shops: list[tuple] = field(default_factory=lambda: list(SHOPS))


def _phone_formats(e164: str, rng: random.Random) -> str:
    d = e164[3:]
    return rng.choice([f"+91 {d[:5]} {d[5:]}", d, f"0{d[:5]}-{d[5:]}", f"+91-{d}", f"{d[:5]} {d[5:]}"])


def _mobile(rng: random.Random) -> str:
    return "+91" + rng.choice("6789") + "".join(rng.choice("0123456789") for _ in range(9))


def _reg(rng: random.Random, city: str) -> str:
    rto = rng.choice(["12", "14"]) if city == "Pune" else rng.choice(["01", "02", "04"])
    return f"MH{rto}{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.choice('ABCDEFGHJKLMNPRSTUVWXYZ')}{rng.randint(1000, 9999)}"


def build_world() -> World:
    rng = random.Random(SEED)
    fake = Faker("en_IN")
    Faker.seed(SEED)
    w = World()
    next_cust = iter(range(3001, 9999, 7))

    def add_customer(name, city="Pune", cust_id=None, phone=None, dob=None, person=None, created=None):
        c = Customer(
            cust_id=cust_id or next(next_cust),
            full_name=name,
            dob=dob or fake.date_of_birth(minimum_age=24, maximum_age=68),
            phone=phone or _phone_formats(_mobile(rng), rng),
            email=f"{name.lower().replace(' ', '.').replace('..', '.')}{rng.randint(1, 99)}@mail.example",
            city=city,
            created_at=created or datetime(2019, 1, 1) + timedelta(days=rng.randint(0, 2200)),
            person=person or f"person-{name.lower().replace(' ', '-')}",
        )
        w.customers.append(c)
        return c

    def add_policy(pol_no, cust, segment, make_model, reg, start, zero_dep=False, history=True):
        idv = {"SUV": rng.randint(9, 16), "SEDAN": rng.randint(6, 11), "HATCH": rng.randint(4, 7)}[segment] * 100000
        snap = dict(pol_no=pol_no, holder_cust_id=cust.cust_id, product="MOTOR_COMP", veh_reg=reg,
                    make_model=make_model, segment=segment, idv=idv, zero_dep=zero_dep,
                    deductible=rng.choice([1000, 2000, 2500, 5000]), start_dt=start.isoformat(),
                    end_dt=(start + timedelta(days=364)).isoformat(), status="ACTIVE", endorsement=None)
        w.policy_events.append(PolicyEvent(pol_no, datetime.combine(start, datetime.min.time()) + timedelta(hours=11), "ISSUE", dict(snap)))
        if history and rng.random() < 0.35:
            when = start + timedelta(days=rng.randint(30, 300))
            snap = dict(snap, deductible=rng.choice([1000, 2000, 5000]), endorsement=f"END-{rng.randint(1000, 4999)}")
            w.policy_events.append(PolicyEvent(pol_no, datetime.combine(when, datetime.min.time()) + timedelta(hours=15), "ENDORSE", dict(snap)))
        w.policies[pol_no] = snap
        return snap

    # --- the claimant, held twice in the CRM ---------------------------------------------
    arjun_dob = date(1988, 4, 17)
    a1 = add_customer("Arjun Malhotra", cust_id=4471, phone="9923055102", dob=arjun_dob,
                      person="person-arjun-malhotra", created=datetime(2021, 6, 3, 10, 5))
    # a long-standing duplicate keyed in by a branch office: mundane, but it splits one person in two
    a2 = add_customer("A. Malhotra", cust_id=9032, phone="+91 99230 55102", dob=arjun_dob,
                      person="person-arjun-malhotra", created=datetime(2023, 2, 11, 15, 40))
    a2.email = "a.malhotra88@mail.example"
    # a different person with the same surname, so a name lookup alone is not enough
    add_customer("Anita Malhotra", cust_id=5120, phone="+91 98500 31277", dob=date(1979, 9, 2))

    # P-2231: the zero-dep add-on arrives the day after the accident
    p2231 = add_policy("P-2231", a1, "SUV", "Hyundai Creta", "MH12AB4321", date(2025, 11, 2), history=False)
    p2231.update(idv=1120000, deductible=2500)
    w.policy_events[-1].snapshot.update(idv=1120000, deductible=2500)
    endorsed = dict(p2231, zero_dep=True, endorsement="END-5512")
    w.policy_events.append(PolicyEvent("P-2231", datetime(2026, 10, 13, 10, 42), "ENDORSE", endorsed))
    w.policies["P-2231"] = endorsed

    # --- the ring: six claims, one shop, one contact phone, thirty days ------------------
    for i, (claim_no, name, filed, loss, amount, shop_written, fir_phone, in_db) in enumerate(RING):
        cust = a2 if claim_no == "CLM-88421" else add_customer(name)
        if claim_no == "CLM-88421":
            pol, reg, model = "P-2231", "MH12AB4321", "Hyundai Creta"
        else:
            model = rng.choice(SEGMENT_MODELS["SUV"])
            reg = _reg(rng, "Pune")
            pol = f"P-{2400 + i * 37}"
            add_policy(pol, cust, "SUV", model, reg, date(2026, 1, 10) + timedelta(days=i * 23), history=False)
        w.claims.append(Claim(claim_no, filed, loss, pol, cust.cust_id, amount, None, None, "OPEN",
                              severity=3, sev_band="B2", loss_type="COLL", veh_reg=reg, shop_gstin=FIXIT_GSTIN,
                              shop_written=shop_written, fir_phone=fir_phone, in_db=in_db,
                              in_sheet=(not in_db) or claim_no == "CLM-88421", has_docs=True,
                              segment="SUV", make_model=model))

    # --- comparable settled collisions, to benchmark the settlement range ---------------
    other_shops = [s for s in SHOPS if s[0] != FIXIT_GSTIN]
    for k, approved in enumerate(range(350000, 455000, 5000)):     # 21 values: P10 = 3.6L, P90 = 4.4L
        cust = add_customer(fake.name())
        model = rng.choice(SEGMENT_MODELS["SUV"])
        reg = _reg(rng, cust.city)
        pol = f"P-{3100 + k}"
        start = date(2024, 6, 1) + timedelta(days=rng.randint(0, 500))
        add_policy(pol, cust, "SUV", model, reg, start)
        loss = start + timedelta(days=rng.randint(20, 330))
        shop = rng.choice(other_shops)
        requested = approved + rng.randint(5, 40) * 1000
        own = _phone_formats(_mobile(rng), rng)
        cust.phone = own
        w.claims.append(Claim(f"CLM-{40000 + rng.randint(100, 9999)}", loss + timedelta(days=rng.randint(1, 4)), loss,
                              pol, cust.cust_id, requested, approved, approved, "PAID", severity=rng.choice([3, 4]),
                              sev_band="B2", loss_type="COLL", veh_reg=reg, shop_gstin=shop[0], shop_written=shop[1],
                              fir_phone=own, in_sheet=rng.random() < 0.3, has_docs=k % 3 == 0,
                              segment="SUV", make_model=model))

    # --- ordinary book of business ------------------------------------------------------
    band_of_sev = {1: "B4", 2: "B4", 3: "B3", 4: "B2", 5: "B1"}
    for k in range(130):
        city = "Mumbai" if rng.random() < 0.25 else "Pune"
        cust = add_customer(fake.name(), city=city)
        segment = rng.choices(["SUV", "SEDAN", "HATCH"], [3, 3, 4])[0]
        model = rng.choice(SEGMENT_MODELS[segment])
        reg = _reg(rng, city)
        pol = f"P-{5000 + k * 3}"
        start = date(2024, 9, 1) + timedelta(days=rng.randint(0, 640))
        add_policy(pol, cust, segment, model, reg, start)
        loss = start + timedelta(days=rng.randint(5, 360))
        if loss >= date(2026, 10, 14):
            loss = date(2026, 10, 14) - timedelta(days=rng.randint(3, 200))
        sev = rng.choices([1, 2, 3, 4, 5], [3, 4, 3, 2, 1])[0]
        loss_type = rng.choices(["COLL", "GLASS", "FLOOD", "THEFT"], [7, 2, 1, 1])[0]
        base = {"SUV": 1.4, "SEDAN": 1.0, "HATCH": 0.7}[segment] * {1: 25000, 2: 60000, 3: 140000, 4: 330000, 5: 700000}[sev]
        requested = int(base * rng.uniform(0.7, 1.3) / 1000) * 1000
        settled = loss < date(2026, 8, 15)
        approved = int(requested * rng.uniform(0.75, 0.98) / 1000) * 1000 if settled else None
        shop = rng.choice(other_shops)
        own = cust.phone
        w.claims.append(Claim(f"CLM-{30000 + k * 211 % 49999 + rng.randint(0, 99)}", loss + timedelta(days=rng.randint(1, 6)),
                              loss, pol, cust.cust_id, requested, approved, approved if settled and rng.random() < 0.9 else None,
                              ("PAID" if settled else "OPEN"), severity=sev, sev_band=band_of_sev[sev], loss_type=loss_type,
                              veh_reg=reg, shop_gstin=shop[0], shop_written=shop[1], fir_phone=own,
                              in_sheet=rng.random() < 0.25, has_docs=k % 9 == 0, segment=segment, make_model=model))

    # two claims keyed in without a policy number: the silver gate holds them
    for c in w.claims[-2:]:
        c.pol_no = None

    # make claim numbers unique (the generator above can collide)
    seen = {c.claim_no for c in w.claims[: len(RING)]}
    for c in w.claims[len(RING):]:
        while c.claim_no in seen:
            c.claim_no = f"CLM-{int(c.claim_no[4:]) + 1}"
        seen.add(c.claim_no)
    return w


def seventh_claim(w: World) -> tuple[Customer, dict, Claim]:
    """The claim that arrives a week later, for the AI-for-Data part of the talk."""
    rng = random.Random(SEED + 1)
    claim_no, name, filed, loss, amount = SEVENTH
    cust = Customer(cust_id=9417, full_name=name, dob=date(1991, 2, 8), phone="+91 97654 22018",
                    email="nikhil.shinde21@mail.example", city="Pune", created_at=datetime(2024, 2, 1),
                    person="person-nikhil-shinde")
    pol = dict(pol_no="P-2719", holder_cust_id=cust.cust_id, product="MOTOR_COMP", veh_reg="MH12KP7781",
               make_model="Kia Seltos", segment="SUV", idv=1250000, zero_dep=False, deductible=2000,
               start_dt="2026-04-03", end_dt="2027-04-02", status="ACTIVE", endorsement=None)
    claim = Claim(claim_no, filed, loss, "P-2719", cust.cust_id, amount, None, None, "OPEN", severity=3,
                  sev_band="B2", loss_type="COLL", veh_reg="MH12KP7781", shop_gstin=FIXIT_GSTIN,
                  shop_written="Fixit Motors near Magarpatta", fir_phone="98220 17643", has_docs=True,
                  segment="SUV", make_model="Kia Seltos")
    del rng
    return cust, pol, claim


def as_dict(obj):
    return asdict(obj)
