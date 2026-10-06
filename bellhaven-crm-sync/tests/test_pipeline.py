"""Unit tests on a small synthetic CRM: SOP paths, idempotency, retries, safety guards."""
import pytest

from bellhaven_sync import apply as applier
from bellhaven_sync import matcher, pipeline, store
from fakes import FakeCRM, acct, loc


@pytest.fixture
def world():
    accounts = [
        {**acct("P1", "Bellhaven Senior Living (Parent Account)", "", parent=""), "care_type": ""},
        {**acct("P2", "Evergreen Care Group (Parent Account)", "", parent=""), "care_type": ""},
        acct("A1", "Bellhaven at Maple Grove", "100 Maple Grove Rd"),                       # confirmed
        acct("A2", "Oak Ridge Assisted Living", "200 Oak Ridge Dr", parent="P2"),           # wrong parent, no AR -> direct
        acct("A3", "Cedar Point Senior Living", "300 Cedar Pt Ave", parent="P2", rev=5000, ar=120),  # CHOW
        acct("A4", "Willow Creek", "400 Willow Creek Lane", rev=100),                        # outdated name; billing -> kept
        acct("A5", "Willow Creek Duplicate Copy", "400 Willow Creek Ln"),                    # duplicate of A4
        acct("A6", "Bellhaven Old Town", "600 Old Town Rd"),                                 # not on website
        acct("A7", "Pine Hill Senior Care", "700 Pine Hill Rd", parent="P2", rev=900, ar=0),  # revenue, no AR -> direct
    ]
    contacts = [{"contact_id": "C1", "account_id": "A5", "name": "Pat Doe", "title": "Administrator",
                 "email": "", "phone": "", "is_active": True}]
    locations = [
        loc("Bellhaven at Maple Grove", "100 Maple Grove Road"),
        loc("Bellhaven Oak Ridge", "200 Oak Ridge Drive"),
        loc("Bellhaven Cedar Point", "300 Cedar Point Avenue"),
        loc("Bellhaven Willow Creek", "400 Willow Creek Lane"),
        loc("Bellhaven Pine Hill", "700 Pine Hill Road"),
        loc("Bellhaven Birchwood", "800 Birch St"),                                          # new
    ]
    return FakeCRM(accounts, contacts), {"locations": locations, "about_text": "We welcomed Evergreen.", "claimed_count": 6}


def by_account(proposals):
    return {p.get("account_id") or p["subject"]: p for p in proposals}


def run(crm, site, conn):
    return pipeline.run(client=crm, conn=conn, site=site)


def approve(conn, crm, pid):
    """Same path as the app's Approve button."""
    assert store.claim(conn, pid)
    result = applier.apply_proposal(conn, crm, store.get_proposal(conn, pid))
    store.set_status(conn, pid, store.APPLIED, result=result)
    return result


def approve_all(conn, crm):
    for p in store.list_proposals(conn, store.PENDING):
        approve(conn, crm, p["id"])


def test_classification(world):
    crm, site = world
    proposals, report = matcher.build_proposals(site["locations"], crm.list_accounts(), crm.list_contacts())
    p = by_account(proposals)
    assert "A1" not in p and any("A1" in c for c in report["confirmed"])
    assert p["A2"]["kind"] == "reparent" and p["A2"]["action"]["sop_path"] == "direct"
    assert p["A3"]["kind"] == "reparent" and p["A3"]["action"]["sop_path"] == "chow"
    assert p["A7"]["action"]["sop_path"] == "direct"  # revenue but zero AR
    assert p["A5"]["kind"] == "duplicate" and p["A5"]["action"]["set"]["duplicate_of_account"] == "A4"
    assert p["A5"]["action"]["move_contacts"] == ["C1"]
    assert p["A4"]["kind"] == "rename"
    assert p["A6"]["kind"] == "not_on_website"
    assert any(x["kind"] == "create" and "Birchwood" in x["title"] for x in proposals)


def test_chow_leaves_old_account_untouched(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    before = crm.get_account("A3")
    result = applier.apply_proposal(conn, crm, p)
    after = crm.get_account("A3")
    new = crm.get_account(result["created_account_id"])
    assert new["parent_id"] == "P1" and new["name"] == "Bellhaven Cedar Point" and new["status"] == "Active"
    assert after["chow_current_account"] == new["account_id"]
    assert {k for k in after if after[k] != before[k]} == {"chow_current_account"}


def test_direct_reparents_existing(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A2")
    applier.apply_proposal(conn, crm, p)
    a2 = crm.get_account("A2")
    assert a2["parent_id"] == "P1" and a2["name"] == "Bellhaven Oak Ridge" and "[bellhaven-sync]" in a2["note"]
    assert not any(w[0] == "create" for w in crm.writes)


def test_sop_rechecked_at_apply_time(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A2")
    crm.accounts["A2"].update(lifetime_revenue=10, outstanding_ar=5)  # billing changed after the proposal
    with pytest.raises(applier.PreconditionFailed):
        applier.apply_proposal(conn, crm, p)
    assert crm.get_account("A2")["parent_id"] == "P2"


def test_rerun_after_approval_proposes_nothing(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    approve_all(conn, crm)
    summary = run(crm, site, conn)
    assert summary["new_for_review"] == 0, store.list_proposals(conn, store.PENDING)
    assert summary["proposals_this_run"] == 0  # CRM now agrees with the website


def test_rejected_items_are_not_reproposed(world):
    crm, site = world
    conn = store.connect(":memory:")
    first = run(crm, site, conn)
    for p in store.list_proposals(conn, store.PENDING):
        store.set_status(conn, p["id"], store.REJECTED)
    second = run(crm, site, conn)
    assert second["new_for_review"] == 0
    assert second["skipped_already_decided"] == first["new_for_review"]
    assert store.list_proposals(conn, store.PENDING) == []


def test_pending_items_not_duplicated_on_rerun(world):
    crm, site = world
    conn = store.connect(":memory:")
    first = run(crm, site, conn)
    second = run(crm, site, conn)
    assert second["new_for_review"] == 0 and second["still_pending"] == first["new_for_review"]


def test_chow_retry_does_not_create_twice(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    assert store.claim(conn, p["id"])
    original = crm.update_account

    def flaky(account_id, fields):
        raise RuntimeError("network blip")

    crm.update_account = flaky
    with pytest.raises(RuntimeError):
        applier.apply_proposal(conn, crm, p)
    crm.update_account = original
    applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"]))
    assert sum(1 for w in crm.writes if w[0] == "create") == 1
    assert crm.get_account("A3")["chow_current_account"]


def test_double_approve_is_harmless(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A5")
    applier.apply_proposal(conn, crm, p)
    n = len(crm.writes)
    result = applier.apply_proposal(conn, crm, p)
    assert result.get("skipped") and len(crm.writes) == n  # second apply writes nothing
    assert crm.get_account("A5")["note"].count("[bellhaven-sync]") == 1


def test_broken_scrape_aborts(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    with pytest.raises(pipeline.ScrapeLooksBroken):
        run(crm, {**site, "locations": site["locations"][:1]}, conn)


def test_money_parsing():
    assert applier.money("$1,234.50") == 1234.5
    assert applier.money(None) == 0 and applier.money("(10.00)") == -10
    assert applier.sop_path({"lifetime_revenue": "$10", "outstanding_ar": "-5"}) == "direct"


# ---------------------------------------------------------------- regressions from the code review


def test_double_click_claims_once(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    pid = store.list_proposals(conn, store.PENDING)[0]["id"]
    assert store.claim(conn, pid) is True
    assert store.claim(conn, pid) is False  # the second click cannot start a second write


def test_create_retry_after_lost_response_reuses_account(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["kind"] == "create")
    real_create = crm.create_account

    def create_then_drop_response(fields):
        real_create(fields)
        raise RuntimeError("connection reset after commit")

    crm.create_account = create_then_drop_response
    with pytest.raises(RuntimeError):
        applier.apply_proposal(conn, crm, p)
    crm.create_account = real_create
    result = applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"]))
    assert result.get("reused_existing_account")
    assert sum(1 for a in crm.accounts.values() if a["name"] == "Bellhaven Birchwood") == 1


def test_chow_never_overwrites_an_existing_pointer(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    crm.accounts["A3"]["chow_current_account"] = "A1"  # billing did a CHOW by hand meanwhile
    with pytest.raises(applier.PreconditionFailed):
        applier.apply_proposal(conn, crm, p)
    assert crm.accounts["A3"]["chow_current_account"] == "A1" and not any(w[0] == "create" for w in crm.writes)


def test_half_finished_chow_finishes_even_if_billing_changed(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    store.save_result(conn, p["id"], {"sop_path": "chow", "created_account_id": "A1"})
    crm.accounts["A3"]["outstanding_ar"] = 0  # AR paid after the new account was created
    result = applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"]))
    assert result["chow_current_account"] == "A1" and crm.accounts["A3"]["chow_current_account"] == "A1"


def test_rejected_duplicate_stays_rejected_after_survivor_rename(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    dup = next(x for x in store.list_proposals(conn) if x["kind"] == "duplicate")
    store.reject(conn, dup["id"])
    rename = next(x for x in store.list_proposals(conn) if x["account_id"] == "A4")
    approve(conn, crm, rename["id"])  # survivor's name changes -> the duplicate's note text changes
    again = run(crm, site, conn)
    assert again["new_for_review"] == 0


def test_stale_proposal_refreshes_its_precondition(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A4")
    crm.accounts["A4"]["name"] = "Willow Creek SL"  # a rep edits the name before approval
    with pytest.raises(applier.PreconditionFailed):
        applier.apply_proposal(conn, crm, p)
    store.set_status(conn, p["id"], store.STALE)
    run(crm, site, conn)
    fresh = store.get_proposal(conn, p["id"])
    assert fresh["status"] == "pending" and fresh["action"]["expect"]["name"] == "Willow Creek SL"
    approve(conn, crm, p["id"])
    assert crm.accounts["A4"]["name"] == "Bellhaven Willow Creek"


def test_contact_moved_elsewhere_is_left_alone(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A5")
    crm.contacts["C1"]["account_id"] = "A1"  # a rep moved the person to another facility
    result = applier.apply_proposal(conn, crm, p)
    assert crm.contacts["C1"]["account_id"] == "A1" and result["contacts_left_in_place"] == ["C1"]


def test_in_flight_write_holds_back_conflicting_proposals(world):
    crm, site = world
    conn = store.connect(":memory:")
    run(crm, site, conn)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    store.claim(conn, p["id"])
    store.set_status(conn, p["id"], store.FAILED, result={"created_account_id": "A1", "error": "boom"})
    summary = run(crm, site, conn)
    assert summary["held_back_for_in_flight_writes"] >= 1
    assert not [x for x in store.list_proposals(conn, store.PENDING) if x["account_id"] in ("A3", "A1")]


def test_billing_copy_under_old_owner_gets_chow_pointer_not_duplicate():
    accounts = [
        {**acct("P1", "Bellhaven Senior Living (Parent Account)", "", parent=""), "care_type": ""},
        {**acct("P2", "Harbor Group (Parent Account)", "", parent=""), "care_type": ""},
        acct("B1", "Bellhaven of Port", "10 Harbor Dr"),
        acct("H1", "Harbor Port Care", "10 Harbor Drive", parent="P2", rev=40000, ar=2500),
    ]
    proposals, _ = matcher.build_proposals([loc("Bellhaven of Port", "10 Harbor Dr")], accounts)
    p = by_account(proposals)
    assert "B1" not in p  # the account already under Bellhaven stays the live record
    assert p["H1"]["kind"] == "chow_duplicate" and p["H1"]["action"]["chow_target_id"] == "B1"


def test_colocated_communities_are_not_merged():
    accounts = [
        {**acct("P1", "Bellhaven Senior Living (Parent Account)", "", parent=""), "care_type": ""},
        acct("W1", "Bellhaven Woods of Toledo", "4850 Sylvania Ave", rev=54000),
        acct("M1", "Bellhaven Memory Care of Toledo", "4850 Sylvania Ave"),
    ]
    locations = [loc("Bellhaven Woods of Toledo", "4850 Sylvania Ave"), loc("Bellhaven Memory Care of Toledo", "4850 Sylvania Ave")]
    proposals, report = matcher.build_proposals(locations, accounts)
    assert proposals == [] and len(report["confirmed"]) == 2


def test_moved_away_ignores_previous_owner_history_records():
    accounts = [
        {**acct("P1", "Bellhaven Senior Living (Parent Account)", "", parent=""), "care_type": ""},
        {**acct("P2", "Cedar Group (Parent Account)", "", parent=""), "care_type": ""},
        acct("NEW", "Bellhaven of Marietta", "805 Colegate Dr"),
        acct("OLD", "Bellhaven of Marietta", "805 Colegate Dr", parent="P2", rev=5, ar=5, chow_current_account="NEW"),
    ]
    proposals, _ = matcher.build_proposals([loc("Bellhaven of Elsewhere", "1 Other Rd")], accounts)
    p = by_account(proposals)
    assert p["NEW"]["kind"] == "not_on_website"  # NOT "moved back to Cedar"


def test_parallel_street_is_not_the_same_facility():
    accounts = [
        {**acct("P1", "Bellhaven Senior Living (Parent Account)", "", parent=""), "care_type": ""},
        {**acct("P3", "Rival Care (Parent Account)", "", parent=""), "care_type": ""},
        acct("R1", "Lakeshore Nursing Center", "2210 Salem Rd", parent="P3", rev=90000),
    ]
    proposals, _ = matcher.build_proposals([loc("The Arbors at Bellhaven", "2210 Salem Ave")], accounts)
    assert [p["kind"] for p in proposals] == ["create"]


def test_review_app_rejects_cross_site_posts(world, monkeypatch, tmp_path):
    from bellhaven_sync import app as review_app, config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    client = review_app.app.test_client()
    assert client.post("/proposal/1/approve", headers={"Origin": "https://evil.example"}).status_code == 403
