"""In-memory stand-in for the CRM API, with the same validation rules the real one enforces."""
import copy

from bellhaven_sync.crm import ACCOUNT_MUTABLE, CRMError

STATUSES = {"Active", "Inactive", "Needs Review"}


class FakeCRM:
    def __init__(self, accounts, contacts=()):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = {c["contact_id"]: copy.deepcopy(c) for c in contacts}
        self.next_id = 0
        self.writes = []

    def list_accounts(self):
        return copy.deepcopy(list(self.accounts.values()))

    def list_contacts(self):
        return copy.deepcopy(list(self.contacts.values()))

    def get_account(self, account_id):
        if account_id not in self.accounts:
            raise CRMError("Account not found")
        return copy.deepcopy(self.accounts[account_id])

    def _validate(self, fields):
        bad = set(fields) - ACCOUNT_MUTABLE
        if bad:
            raise CRMError(f"not mutable: {bad}")
        if "status" in fields and fields["status"] not in STATUSES:
            raise CRMError("bad status")
        for ref in ("parent_id", "chow_current_account", "duplicate_of_account"):
            if fields.get(ref) and fields[ref] not in self.accounts:
                raise CRMError(f"{ref} does not exist")

    def update_account(self, account_id, fields):
        self._validate(fields)
        self.writes.append(("update", account_id, dict(fields)))
        self.accounts[account_id].update(fields)
        return self.get_account(account_id)

    def create_account(self, fields):
        if not fields.get("name"):
            raise CRMError("name is required")
        self._validate(fields)
        self.next_id += 1
        acct = {"account_id": f"001NEW{self.next_id:06d}", "parent_id": "", "billing_street": "", "billing_city": "",
                "billing_state": "", "billing_zip": "", "care_type": "", "status": "Active", "phone": "",
                "lifetime_revenue": 0, "outstanding_ar": 0, "chow_current_account": "", "duplicate_of_account": "",
                "note": "", **fields}
        self.writes.append(("create", acct["account_id"], dict(fields)))
        self.accounts[acct["account_id"]] = acct
        return copy.deepcopy(acct)

    def update_contact(self, contact_id, fields):
        if fields.get("account_id") and fields["account_id"] not in self.accounts:
            raise CRMError("account_id does not exist")
        self.writes.append(("contact", contact_id, dict(fields)))
        self.contacts[contact_id].update(fields)
        return copy.deepcopy(self.contacts[contact_id])


def acct(id, name, street, city="Columbus", state="OH", zip="43215", parent="P1", status="Active", rev=0, ar=0, **kw):
    return {"account_id": id, "name": name, "parent_id": parent, "billing_street": street, "billing_city": city,
            "billing_state": state, "billing_zip": zip, "care_type": "Assisted Living", "status": status,
            "phone": "", "lifetime_revenue": rev, "outstanding_ar": ar, "chow_current_account": "",
            "duplicate_of_account": "", "note": "", **kw}


def loc(name, address, city="Columbus", state="OH", zip="43215", **kw):
    return {"name": name, "address": address, "city": city, "state": state, "zip": zip,
            "care_offerings": ["Assisted Living"], "phone": "", "administrator": "", "url": "https://example/x",
            "found_on": ["directory"], **kw}
