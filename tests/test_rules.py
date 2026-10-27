"""Unit tests for the rules the demo's claims rest on. No services needed."""

from claims import governance
from claims.layers.f_signals import fraud_score
from claims.normalize import claim_id, ddmmyy, mask_phone, phone_e164, shop_key


def test_phone_formats_resolve_to_one_number():
    formats = ["+91 98220 17643", "098220-17643", "9822017643", "+91-9822017643", "98220 17643"]
    assert {phone_e164(p) for p in formats} == {"+919822017643"}


def test_phone_mask_matches_the_deck():
    assert mask_phone("+919822017643") == "+91 98••• 43"


def test_claim_references_standardise():
    assert claim_id("CLM 88421") == claim_id("CLM-88421") == claim_id("claim number 88421") == "CLM-88421"


def test_dates_from_both_systems():
    assert ddmmyy("14/10/26") == ddmmyy("2026-10-14")


def test_shop_spellings_normalise_to_one_key():
    spellings = ["FixIt Motors", "Fixit Motor Works", "FIXIT MOTORS PVT. LTD.", "Fix-It Motors, Hadapsar", "Fixit Motors near Magarpatta"]
    assert {shop_key(s) for s in spellings} == {"fixit motors"}


def test_fraud_model_gives_the_deck_score_for_the_case():
    case = {"shared_contact_claims_30d": 5, "shop_claims_30d": 5, "contact_phone_not_claimant": 1, "post_loss_endorsement": 1}
    assert fraud_score(case) == 0.82
    ordinary = {"shared_contact_claims_30d": 0, "shop_claims_30d": 1, "contact_phone_not_claimant": 0, "post_loss_endorsement": 0}
    assert fraud_score(ordinary) < 0.1


def test_payments_need_a_person_but_routing_does_not():
    officer = governance.Principal(role="claims_officer")
    assert governance.gate_action("fast_track_settlement", officer)["status"] == "pending_approval"
    assert governance.gate_action("route_to_investigation", officer)["status"] == "done"
    assert not governance.gate_action("approve_settlement", governance.Principal(role="investigator"))["allowed"]


def test_tables_touched_by_sql_are_audited():
    assert governance.touched("select * from silver.claims c join gold.claim_signals s using (claim_id)") == \
        ["gold.claim_signals", "silver.claims"]
