"""Execute an approved proposal against the CRM.

Only this module writes to the CRM, and only the review app calls it, after a
human clicks Approve. Before writing it re-reads the account and refuses if the
CRM no longer looks the way it did when the proposal was made. Every step is
safe to retry: values already in place are skipped, a create first looks for
an account already at that address, and a created id is saved before the next
step runs.

Ops:
  update        PATCH fields on one account (+ note, + move a duplicate's contacts)
  create        new account for a website location
  reparent      move an account to a new parent, via the billing SOP
                (direct re-parent, or CHOW: new account + pointer on the old one)
  chow_pointer  SOP CHOW where the current-owner account already exists:
                set only chow_current_account on the old account
"""
from datetime import date

from . import config, store
from . import normalize as N

DIRECT = "direct"
CHOW = "chow"
ID = "account_id"


def money(value):
    """Parse a billing field (number, None, or '$1,234.50') into a float."""
    if value in (None, ""):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace("$", "").replace(",", "").strip()
    if cleaned.startswith("(") and cleaned.endswith(")"):  # accounting negative
        cleaned = "-" + cleaned[1:-1]
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def sop_path(account):
    """Billing SOP for moving an account to a different parent.

    Revenue history AND outstanding AR > 0  -> CHOW: keep the old account
    exactly as it is, put the facility on an account under the new parent,
    and point the old account's chow_current_account at it.
    Anything else                           -> re-parent the existing account.
    """
    revenue = money(account.get("lifetime_revenue"))
    ar = money(account.get("outstanding_ar"))
    return CHOW if revenue > 0 and ar > 0 else DIRECT


def append_note(existing, text):
    line = f"{date.today().isoformat()} {config.NOTE_TAG} {text}"
    return f"{existing.rstrip()}\n{line}" if existing and existing.strip() else line


class PreconditionFailed(RuntimeError):
    pass


def same(a, b):
    norm = lambda v: "" if v is None else str(v).strip()
    return norm(a) == norm(b)


def _check_expected(account, expect):
    drift = {k: {"expected": v, "now": account.get(k)} for k, v in expect.items() if not same(account.get(k), v)}
    if drift:
        raise PreconditionFailed(f"CRM changed since this proposal was made: {drift}")


def _require_sop(account, wanted):
    path_now = sop_path(account)
    if path_now != wanted:
        raise PreconditionFailed(
            f"Billing data changed: SOP path is now '{path_now}', proposal was '{wanted}'. "
            "Re-run the pipeline for a fresh proposal."
        )


def _existing_at_address(client, fields):
    """An account already under the target parent at this street + ZIP (a lost-response retry, or a rep got there first)."""
    for a in client.find_accounts(zip=N.zip5(fields["billing_zip"]), parent_id=fields["parent_id"]):
        if N.street(a.get("billing_street")) == N.street(fields["billing_street"]):
            return a
    return None


def _create_once(client, fields, note):
    existing = _existing_at_address(client, fields)
    if existing:
        return existing[ID], {"reused_existing_account": existing[ID]}
    created = client.create_account({**fields, "note": append_note(None, note)})
    return created[ID], {"created": created}


def _update(client, account, set_fields, note, expect):
    """PATCH only what is not already in place; append the note once."""
    pending = {k: v for k, v in set_fields.items() if not same(account.get(k), v)}
    if set_fields and not pending:
        return {"skipped": "already applied", "account_id": account[ID]}
    if not set_fields and note and note in (account.get("note") or ""):
        return {"skipped": "note already present", "account_id": account[ID]}
    _check_expected(account, expect)
    fields = dict(pending)
    if note:
        fields["note"] = append_note(account.get("note"), note)
    after = client.update_account(account[ID], fields)
    return {"account_id": account[ID], "fields": fields, "after": after}


def _move_contacts(client, contact_ids, from_id, to_id):
    moved, skipped = [], []
    for cid in contact_ids:
        contact = client.get_contact(cid)
        if contact["account_id"] == from_id:
            client.update_contact(cid, {"account_id": to_id})
            moved.append(cid)
        elif contact["account_id"] != to_id:  # a rep moved it elsewhere since: leave it there
            skipped.append(cid)
    return moved, skipped


def _set_pointer(client, account, target_id):
    current = account.get("chow_current_account")
    if same(current, target_id):
        return {"skipped": "CHOW pointer already set", "old_account_id": account[ID], "chow_current_account": target_id}
    if current:
        raise PreconditionFailed(f"{account[ID]} already points at {current}; refusing to overwrite with {target_id}.")
    after = client.update_account(account[ID], {"chow_current_account": target_id})
    return {"old_account_id": account[ID], "chow_current_account": target_id, "old_after": after}


def apply_proposal(conn, client, proposal):
    """Write one approved proposal. Returns a result dict; raises on failure."""
    action = proposal["action"]
    prior = {k: v for k, v in (proposal.get("result") or {}).items() if k not in ("error", "trace")}
    op = action["op"]

    def save(progress):
        if conn is not None and proposal.get("id") is not None:
            store.save_result(conn, proposal["id"], progress)

    if op == "create":
        if prior.get("created_account_id"):
            return prior
        new_id, info = _create_once(client, action["fields"], action["note"])
        result = {"created_account_id": new_id, **info}
        save(result)
        return result

    account = client.get_account(action["account_id"])

    if op == "update":
        result = _update(client, account, action.get("set", {}), action.get("note"), action.get("expect", {}))
        if action.get("move_contacts"):
            moved, skipped = _move_contacts(client, action["move_contacts"], account[ID], action["move_contacts_to"])
            result.update(moved_contacts=moved, contacts_left_in_place=skipped)
        return result

    if op == "chow_pointer":
        if not same(account.get("chow_current_account"), action["chow_target_id"]):
            _check_expected(account, action.get("expect", {}))
            _require_sop(account, CHOW)
        return {"sop_path": CHOW, **_set_pointer(client, account, action["chow_target_id"])}

    if op == "reparent":
        new_id = prior.get("created_account_id")
        if new_id:  # a CHOW already created the new account: finish it, whatever billing did since
            return {**prior, **_set_pointer(client, account, new_id)}
        if same(account.get("parent_id"), action["new_parent_id"]):
            return {"skipped": "already under target parent", "account_id": account[ID]}
        _check_expected(account, action.get("expect", {}))
        _require_sop(account, action["sop_path"])

        if action["sop_path"] == DIRECT:
            fields = {"parent_id": action["new_parent_id"], **action.get("set", {})}
            fields["note"] = append_note(account.get("note"), action["note"])
            after = client.update_account(account[ID], fields)
            return {"sop_path": DIRECT, "account_id": account[ID], "fields": fields, "after": after}

        # CHOW: new account under the new parent; the old account keeps everything except the pointer.
        new_fields = {**action["new_account"], "parent_id": action["new_parent_id"]}
        new_id, info = _create_once(client, new_fields, f"Created by CHOW from account {account[ID]}. {action['note']}")
        progress = {"sop_path": CHOW, "created_account_id": new_id, **info}
        save(progress)
        return {**progress, **_set_pointer(client, account, new_id)}

    raise ValueError(f"Unknown op {op!r}")
