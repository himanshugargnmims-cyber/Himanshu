"""Regression tests on the real 2026-10-06 snapshot of the website and CRM sandbox.

Each assertion pins one of the traps found in the data, so a matcher change that
breaks one of them fails loudly (useful when making live changes in the demo).
"""
import json
from pathlib import Path

import pytest

from bellhaven_sync import apply as applier
from bellhaven_sync import matcher, pipeline, store
from fakes import FakeCRM

FIX = Path(__file__).parent / "fixtures"
SITE = json.loads((FIX / "site_2026-10-06.json").read_text())
ACCOUNTS = json.loads((FIX / "accounts_2026-10-06.json").read_text())
CONTACTS = json.loads((FIX / "contacts_2026-10-06.json").read_text())

BELLHAVEN = "0015QAPLGS3FVYEEEM"
MILLSTONE_SANDUSKY = "0017JP8Z1UQ763BVK3"


@pytest.fixture(scope="module")
def result():
    proposals, report = matcher.build_proposals(SITE["locations"], ACCOUNTS, CONTACTS, SITE["about_text"])
    by_acct = {}
    for p in proposals:
        by_acct.setdefault(p.get("account_id") or p["subject"], []).append(p)
    return proposals, report, by_acct


def kind(by_acct, account_id):
    ps = by_acct.get(account_id, [])
    assert len(ps) == 1, f"{account_id}: {[p['kind'] for p in ps]}"
    return ps[0]


def test_scrape_found_homepage_only_community():
    assert len(SITE["locations"]) == SITE["claimed_count"] == 35
    findlay = next(l for l in SITE["locations"] if "Findlay" in l["name"])
    assert findlay["found_on"] == ["page:/"]


def test_creates_only_genuinely_new_facilities(result):
    proposals, _, _ = result
    created = sorted(p["action"]["fields"]["name"] for p in proposals if p["kind"] == "create")
    assert created == ["Amberly Manor", "Bellhaven at Union Square", "Bellhaven of Batavia", "Bellhaven of Carlisle"]
    for p in proposals:
        if p["kind"] == "create":
            assert p["action"]["fields"]["parent_id"] == BELLHAVEN


def test_lookalikes_are_never_touched(result):
    _, report, by_acct = result
    for lookalike in ("001GNU41AVXZRLLJ9P",   # Union Square Senior Living (Juniper Point, other address)
                      "0015D74ZLRY810RGY5",   # Amberly Manor, Colorado Springs
                      "0019M1Z45PGV2DJ971"):  # Maplewood Senior Care Center (other address)
        assert lookalike not in by_acct
    assert len(report["lookalikes_rejected"]) == 3


def test_billing_sop(result):
    _, _, by_acct = result
    for chow in ("001A34WFSUYHCRBLFT", "001U6RW32TY0WSXZZB"):   # Marietta, Tiffin: revenue AND AR
        p = kind(by_acct, chow)
        assert (p["kind"], p["action"]["sop_path"]) == ("reparent", "chow")
        assert p["action"]["new_account"]["name"].startswith("Bellhaven of")
    for direct in ("001LGFPBJY4N9MB6KL",   # Lima: revenue, no AR
                   "001UKEFGADQ8YCZ4YM",   # Findlay: no parent, revenue, no AR
                   "001H1JMVZWP46D5VUF"):  # Zanesville: no revenue
        p = kind(by_acct, direct)
        assert (p["kind"], p["action"]["sop_path"]) == ("reparent", "direct")
    assert "0019Y4J61ZBG4R00CD" not in by_acct  # Akron has AR but is already under Bellhaven: no move, no SOP


def test_sandusky_points_at_existing_millstone_account(result):
    _, _, by_acct = result
    p = kind(by_acct, "001SXSF4ELF0Z2LGDM")
    assert p["kind"] == "moved_away" and p["action"]["op"] == "chow_pointer"
    assert p["action"]["chow_target_id"] == MILLSTONE_SANDUSKY and p["evidence"]["sop"]["path"] == "chow"


def test_duplicates_point_at_one_survivor_per_facility(result):
    proposals, _, by_acct = result
    dups = {p["account_id"]: p["action"]["set"]["duplicate_of_account"] for p in proposals if p["kind"] == "duplicate"}
    assert dups == {
        "00159PL81N38KM4FHM": "001U1750VLVJAGG1S5",  # Monroe (Harborview) -> Bellhaven Gardens of Monroe
        "0011AB44D05WLA9HTX": "001U1750VLVJAGG1S5",  # Monroe (Cedar Trail)
        "001WR41PYNWXCAE2X4": "0016KTS1UAWBRXS09J",  # Kettering (Harborview) -> Kettering N&R
        "001B7XZAA3AFALS9GP": "0016KTS1UAWBRXS09J",  # Kettering (Cedar Trail, "Wilmington Pk")
        "001QU150PM4Z15UA71": "001EGU7BMJ942ZTRE6",  # Owosso: website admin + phone confirm the kept copy
        "001JD2MWRA74LTSN24": "001UELXDAKFRKB8932",  # Port Clinton (Harborview)
        "001BLYF02K97SZLZHH": "001CVBBCSDM7YHN220",  # Erie (Harborview)
    }
    assert set(dups.values()).isdisjoint(dups)  # no survivor is itself marked duplicate
    assert by_acct["001QU150PM4Z15UA71"][0]["action"]["move_contacts"]  # Owosso duplicate's contact moves


def test_field_fixes(result):
    _, _, by_acct = result
    assert kind(by_acct, "001CF3LDWVRGL09P4F")["action"]["set"] == {"billing_zip": "45662"}  # Portsmouth zip typo
    ashtabula = kind(by_acct, "001NXP9X46CWEPSLSV")
    assert ashtabula["kind"] == "annotate" and ashtabula["action"]["set"] == {}  # keep PO Box billing address
    assert kind(by_acct, "001RJU1X4NBWC1Q0G7")["action"]["set"] == {"name": "Bellhaven of Chagrin Falls"}
    assert kind(by_acct, "0017MN2JYAJBDS8WQZ")["action"]["set"] == {"name": "Bellhaven Willow Creek"}


def test_unlisted_and_acquired_operators(result):
    _, _, by_acct = result
    for gone in ("00116ETS45BL7DTQP7", "0016PVXH4B25HWR7QE"):  # Alliance, Coldwater
        p = kind(by_acct, gone)
        assert p["kind"] == "not_on_website" and p["action"]["set"] == {"status": "Needs Review"}
    for operator in ("001FJZYHR7MLFMNPLL", "001FWSQ30SFW6S7604"):  # Harborview, Cedar Trail: note only, status untouched
        p = kind(by_acct, operator)
        assert p["kind"] == "operator_note" and p["action"]["set"] == {} and "About page" in p["action"]["note"]
    for untouched in ("00139TNDS8HNLUZ5A6", "001DAAUWV2J3SHQJ34", "001YRHHXQ5HJ0TCL2U"):  # not mentioned on About page
        assert untouched not in by_acct


def test_incomplete_scrape_suppresses_gone_checks():
    proposals, _ = matcher.build_proposals(SITE["locations"], ACCOUNTS, CONTACTS, SITE["about_text"], site_complete=False)
    assert not [p for p in proposals if p["kind"] in ("not_on_website", "moved_away")]


def test_full_approval_end_state_and_idempotent_rerun():
    crm = FakeCRM(ACCOUNTS, CONTACTS)
    conn = store.connect(":memory:")
    first = pipeline.run(client=crm, conn=conn, site=SITE)
    assert first["new_for_review"] == 31
    for p in store.list_proposals(conn, store.PENDING):
        assert store.claim(conn, p["id"])
        store.set_status(conn, p["id"], store.APPLIED, result=applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"])))

    acc = crm.accounts
    # Every website location now has exactly one live account under Bellhaven.
    live = [a for a in acc.values() if a["parent_id"] == BELLHAVEN and a["status"] == "Active"
            and not a["duplicate_of_account"] and not a["chow_current_account"]]
    assert sorted(a["name"] for a in live) == sorted(l["name"] for l in SITE["locations"])
    # CHOW old accounts untouched apart from the pointer.
    for old_id in ("001A34WFSUYHCRBLFT", "001U6RW32TY0WSXZZB", "001SXSF4ELF0Z2LGDM"):
        before = next(a for a in ACCOUNTS if a["account_id"] == old_id)
        changed = {k for k in before if acc[old_id][k] != before[k]}
        assert changed == {"chow_current_account"}, (old_id, changed)
    assert acc["001SXSF4ELF0Z2LGDM"]["chow_current_account"] == MILLSTONE_SANDUSKY

    second = pipeline.run(client=crm, conn=conn, site=SITE)
    assert second["proposals_this_run"] == 0 and second["new_for_review"] == 0
