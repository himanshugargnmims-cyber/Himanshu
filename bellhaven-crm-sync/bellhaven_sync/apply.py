"""Execute an approved proposal against the CRM.

Only this module writes to the CRM, and only the review app calls it, after a
human clicks Approve. Every write re-reads the account first and refuses to
proceed if the CRM no longer looks the way it did when the proposal was made.
Every step is safe to retry: values already in place are skipped, and a
created account id is saved before the next step runs.
"""
from datetime import date

from . import config, store

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


def _save_progress(conn, proposal, result):
    if conn is not None and proposal.get("id") is not None:
        store.set_status(conn, proposal["id"], store.APPROVED, result=result)


def apply_proposal(conn, client, proposal):
    """Write one approved proposal. Returns a result dict; raises on failure."""
    action = proposal["action"]
    prior = dict(proposal.get("result") or {})
    prior.pop("error", None)
    prior.pop("trace", None)
    op = action["op"]

    if op == "create":
        if prior.get("created_account_id"):  # retry after a partial failure
            return prior
        fields = dict(action["fields"])
        fields["note"] = append_note(None, action["note"])
        created = client.create_account(fields)
        result = {"created_account_id": created[ID], "created": created}
        _save_progress(conn, proposal, result)
        return result

    account = client.get_account(action["account_id"])

    if op == "update":
        result = _update(client, account, action.get("set", {}), action.get("note"), action.get("expect", {}))
        moved = []
        for contact_id in action.get("move_contacts", []):
            client.update_contact(contact_id, {"account_id": action["move_contacts_to"]})
            moved.append(contact_id)
        if moved:
            result["moved_contacts"] = moved
        return result

    if op == "reparent":
        target_parent = action["new_parent_id"]
        if same(account.get("parent_id"), target_parent):
            return {"skipped": "already under target parent", "account_id": account[ID]}
        if account.get("chow_current_account") and action["sop_path"] == CHOW:
            if prior.get("created_account_id") or action.get("chow_target_id"):
                return {**prior, "skipped": "CHOW pointer already set", "old_account_id": account[ID]}
        _check_expected(account, action.get("expect", {}))
        # Re-evaluate the SOP on live billing data: the reviewer approved a specific path.
        path_now = sop_path(account)
        if path_now != action["sop_path"]:
            raise PreconditionFailed(
                f"Billing data changed: SOP path is now '{path_now}', proposal was '{action['sop_path']}'. "
                "Re-run the pipeline to get a fresh proposal."
            )
        if path_now == DIRECT:
            fields = {"parent_id": target_parent, **action.get("set", {})}
            fields["note"] = append_note(account.get("note"), action["note"])
            after = client.update_account(account[ID], fields)
            return {"sop_path": DIRECT, "account_id": account[ID], "fields": fields, "after": after}

        # CHOW. The old account keeps every field as-is except the pointer.
        new_id = action.get("chow_target_id") or prior.get("created_account_id")
        if not new_id:
            fields = dict(action["new_account"])
            fields["parent_id"] = target_parent
            fields["note"] = append_note(None, f"Created by CHOW from account {account[ID]}. {action['note']}")
            created = client.create_account(fields)
            new_id = created[ID]
            prior = {"sop_path": CHOW, "created_account_id": new_id, "created": created}
            _save_progress(conn, proposal, prior)
        after = client.update_account(account[ID], {"chow_current_account": new_id})
        return {**prior, "sop_path": CHOW, "chow_current_account": new_id, "old_account_id": account[ID], "old_after": after}

    raise ValueError(f"Unknown op {op!r}")
