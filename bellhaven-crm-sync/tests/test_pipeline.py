import copy

import pytest

from bellhaven_sync import apply as applier
from bellhaven_sync import matcher, pipeline, store


class FakeCRM:
    def __init__(self, accounts):
        self.accounts = {str(a["id"]): copy.deepcopy(a) for a in accounts}
        self.next_id = 1000
        self.writes = []

    def list_accounts(self):
        return copy.deepcopy(list(self.accounts.values()))

    def get_account(self, account_id):
        return copy.deepcopy(self.accounts[str(account_id)])

    def update_account(self, account_id, fields):
        self.writes.append(("update", str(account_id), fields))
        self.accounts[str(account_id)].update(fields)
        return copy.deepcopy(self.accounts[str(account_id)])

    def create_account(self, fields):
        self.next_id += 1
        acct = {"id": str(self.next_id), "lifetime_revenue": 0, "outstanding_ar": 0,
                "duplicate_of_account": None, "chow_current_account": None, **fields}
        self.writes.append(("create", acct["id"], fields))
        self.accounts[acct["id"]] = acct
        return copy.deepcopy(acct)


def acct(id, name, address, city="Columbus", state="OH", zip="43215", parent="P1", status="Active",
         rev=0, ar=0, **kw):
    return {"id": id, "name": name, "address": address, "city": city, "state": state, "zip": zip,
            "parent_id": parent, "status": status, "note": "", "lifetime_revenue": rev, "outstanding_ar": ar,
            "duplicate_of_account": None, "chow_current_account": None, **kw}


def loc(name, address, city="Columbus", state="OH", zip="43215"):
    return {"name": name, "address": address, "city": city, "state": state, "zip": zip,
            "care_offerings": ["Assisted Living"], "url": "https://example/x"}


@pytest.fixture
def world():
    accounts = [
        {"id": "P1", "name": "Bellhaven Senior Living", "parent_id": None, "status": "Active"},
        {"id": "P2", "name": "Evergreen Care Group", "parent_id": None, "status": "Active"},
        acct("A1", "Bellhaven at Maple Grove", "100 Maple Grove Rd"),                      # confirmed
        acct("A2", "Oak Ridge Assisted Living", "200 Oak Ridge Dr", parent="P2"),          # wrong parent, no AR -> direct
        acct("A3", "Cedar Point Senior Living", "300 Cedar Pt Ave", parent="P2", rev=5000, ar=120),  # CHOW
        acct("A4", "Willow Creek", "400 Willow Creek Ln"),                                  # outdated name
        acct("A5", "Willow Creek Duplicate Copy", "400 Willow Creek Lane"),                 # duplicate of A4
        acct("A6", "Bellhaven Old Town", "600 Old Town Rd"),                                # not on website
        acct("A7", "Pine Hill Senior Care", "700 Pine Hill Rd", parent="P2", rev=900, ar=0),  # revenue but no AR -> direct
    ]
    locations = [
        loc("Bellhaven at Maple Grove", "100 Maple Grove Road"),
        loc("Bellhaven Oak Ridge", "200 Oak Ridge Drive"),
        loc("Bellhaven Cedar Point", "300 Cedar Point Avenue"),
        loc("Bellhaven Willow Creek", "400 Willow Creek Lane"),
        loc("Bellhaven Pine Hill", "700 Pine Hill Road"),
        loc("Bellhaven Birchwood", "800 Birch St"),                                         # new
    ]
    return FakeCRM(accounts), locations


def by_account(proposals):
    return {p.get("account_id") or p["subject"]: p for p in proposals}


def test_classification(world):
    crm, locations = world
    proposals, report = matcher.build_proposals(locations, crm.list_accounts())
    p = by_account(proposals)
    assert "A1" not in p and any("A1" in c for c in report["confirmed"])
    assert p["A2"]["kind"] == "reparent" and p["A2"]["action"]["sop_path"] == "direct"
    assert p["A3"]["kind"] == "reparent" and p["A3"]["action"]["sop_path"] == "chow"
    assert p["A7"]["action"]["sop_path"] == "direct"  # revenue but zero AR
    assert p["A5"]["kind"] == "duplicate" and p["A5"]["action"]["set"]["duplicate_of_account"] == "A4"
    assert p["A4"]["kind"] == "rename"
    assert p["A6"]["kind"] == "not_on_website"
    assert any(x["kind"] == "create" and "Birchwood" in x["title"] for x in proposals)


def test_sop_chow_leaves_old_account_untouched(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    before = crm.get_account("A3")
    result = applier.apply_proposal(conn, crm, p)
    after = crm.get_account("A3")
    new = crm.get_account(result["created_account_id"])
    assert new["parent_id"] == "P1" and new["name"] == "Bellhaven Cedar Point"
    assert after["chow_current_account"] == new["id"]
    # Exactly one field changed on the old account.
    assert {k for k in after if after[k] != before[k]} == {"chow_current_account"}


def test_sop_direct_reparents_existing(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A2")
    applier.apply_proposal(conn, crm, p)
    a2 = crm.get_account("A2")
    assert a2["parent_id"] == "P1" and a2["name"] == "Bellhaven Oak Ridge"
    assert not any(w[0] == "create" for w in crm.writes)


def test_sop_rechecked_at_apply_time(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A2")
    crm.accounts["A2"].update(lifetime_revenue=10, outstanding_ar=5)  # billing changed after proposal
    with pytest.raises(applier.PreconditionFailed):
        applier.apply_proposal(conn, crm, p)
    assert crm.get_account("A2")["parent_id"] == "P2"


def _approve_all(conn, crm):
    for p in store.list_proposals(conn, store.PENDING):
        store.set_status(conn, p["id"], store.APPROVED)
        result = applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"]))
        store.set_status(conn, p["id"], store.APPLIED, result=result)


def test_rerun_after_approval_proposes_nothing(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    _approve_all(conn, crm)
    summary = pipeline.run(client=crm, conn=conn, locations=locations)
    assert summary["new_for_review"] == 0, store.list_proposals(conn, store.PENDING)
    assert summary["proposals_this_run"] == 0  # CRM now agrees with the website


def test_rejected_items_are_not_reproposed(world):
    crm, locations = world
    conn = store.connect(":memory:")
    first = pipeline.run(client=crm, conn=conn, locations=locations)
    for p in store.list_proposals(conn, store.PENDING):
        store.set_status(conn, p["id"], store.REJECTED)
    second = pipeline.run(client=crm, conn=conn, locations=locations)
    assert second["new_for_review"] == 0
    assert second["skipped_already_decided"] == first["new_for_review"]
    assert store.list_proposals(conn, store.PENDING) == []


def test_pending_items_not_duplicated_on_rerun(world):
    crm, locations = world
    conn = store.connect(":memory:")
    first = pipeline.run(client=crm, conn=conn, locations=locations)
    second = pipeline.run(client=crm, conn=conn, locations=locations)
    assert second["new_for_review"] == 0 and second["still_pending"] == first["new_for_review"]


def test_chow_retry_does_not_create_twice(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    p = next(x for x in store.list_proposals(conn) if x["account_id"] == "A3")
    original_update = crm.update_account

    def flaky(account_id, fields):
        raise RuntimeError("network blip")

    crm.update_account = flaky
    with pytest.raises(RuntimeError):
        applier.apply_proposal(conn, crm, p)
    crm.update_account = original_update
    applier.apply_proposal(conn, crm, store.get_proposal(conn, p["id"]))
    assert sum(1 for w in crm.writes if w[0] == "create") == 1


def test_broken_scrape_aborts(world):
    crm, locations = world
    conn = store.connect(":memory:")
    pipeline.run(client=crm, conn=conn, locations=locations)
    with pytest.raises(pipeline.ScrapeLooksBroken):
        pipeline.run(client=crm, conn=conn, locations=locations[:1])


def test_money_parsing():
    assert applier.money("$1,234.50") == 1234.5
    assert applier.money(None) == 0 and applier.money("(10.00)") == -10
    assert applier.sop_path({"lifetime_revenue": "$10", "outstanding_ar": "-5"}) == "direct"
