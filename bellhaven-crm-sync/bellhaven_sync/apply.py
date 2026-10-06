"""Execute an approved proposal against the CRM.

Only this module writes to the CRM, and only the review app calls it, after a
human clicks Approve. Every write re-reads the account first and refuses to
proceed if the CRM no longer looks the way it did when the proposal was made.
"""
from datetime import date

from . import config, store

DIRECT = "direct"
CHOW = "chow"


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
    untouched, create a new one under the right parent, point the old one at it.
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


def _same(a, b):
    norm = lambda v: "" if v is None else str(v).strip()
    return norm(a) == norm(b)


def _check_expected(account, expect):
    drift = {k: {"expected": v, "now": account.get(k)} for k, v in expect.items() if not _same(account.get(k), v)}
    if drift:
        raise PreconditionFailed(f"CRM changed since this proposal was made: {drift}")


def apply_proposal(conn, client, proposal):
    """Write one approved proposal. Returns a result dict; raises on failure."""
    action = proposal["action"]
    prior = proposal.get("result") or {}
    op = action["op"]

    if op == "create":
        if prior.get("created_account_id"):  # retry after a partial failure
            return prior
        fields = dict(action["fields"])
        fields["note"] = append_note(fields.get("note"), action["note"])
        created = client.create_account(fields)
        result = {"created_account_id": created["id"], "created": created}
        store.set_status(conn, proposal["id"], store.APPROVED, result=result)
        return result

    account = client.get_account(action["account_id"])
    _check_expected(account, action.get("expect", {}))

    if op == "update":
        fields = dict(action["set"])
        if action.get("note"):
            fields["note"] = append_note(account.get("note"), action["note"])
        updated = client.update_account(account["id"], fields)
        return {"updated_account_id": account["id"], "fields": fields, "after": updated}

    if op == "reparent":
        # Re-evaluate the SOP on fresh billing data; the reviewer approved a specific path.
        path_now = sop_path(account)
        if path_now != action["sop_path"]:
            raise PreconditionFailed(
                f"Billing data changed: SOP path is now '{path_now}', proposal was '{action['sop_path']}'. "
                "Re-run the pipeline to get a fresh proposal."
            )
        if path_now == DIRECT:
            fields = {"parent_id": action["new_parent_id"], **action.get("set", {})}
            fields["note"] = append_note(account.get("note"), action["note"])
            updated = client.update_account(account["id"], fields)
            return {"sop_path": DIRECT, "updated_account_id": account["id"], "fields": fields, "after": updated}

        # CHOW: create the replacement account, then ONLY set the pointer on the old one.
        new_id = prior.get("created_account_id")
        if not new_id:
            fields = dict(action["new_account"])
            fields["parent_id"] = action["new_parent_id"]
            fields["note"] = append_note(
                None, f"Created by CHOW from account {account['id']}. {action['note']}"
            )
            created = client.create_account(fields)
            new_id = created["id"]
            prior = {"sop_path": CHOW, "created_account_id": new_id, "created": created}
            store.set_status(conn, proposal["id"], store.APPROVED, result=prior)
        updated = client.update_account(account["id"], {"chow_current_account": new_id})
        return {**prior, "old_account_id": account["id"], "old_after": updated}

    raise ValueError(f"Unknown op {op!r}")
