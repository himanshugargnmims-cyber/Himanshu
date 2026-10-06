"""Link website locations to CRM accounts and turn every discrepancy into a proposal.

A proposal is a dict:
  kind       reparent | update | duplicate | create | not_on_website
  subject    stable identity used in the idempotency fingerprint
  action     exactly what apply.py will write (also part of the fingerprint)
  evidence   what the reviewer sees (website vs CRM, signals, SOP check, plan)
  confidence high | medium | low
"""
from dataclasses import dataclass, field

from . import normalize as N
from .apply import CHOW, DIRECT, money, sop_path

ACTIVE, INACTIVE, NEEDS_REVIEW = "Active", "Inactive", "Needs Review"

# CRM field names (one place to change if the API differs).
F_NAME, F_ADDR, F_CITY, F_STATE, F_ZIP = "name", "address", "city", "state", "zip"
F_PARENT, F_STATUS, F_NOTE = "parent_id", "status", "note"
F_DUP, F_CHOW = "duplicate_of_account", "chow_current_account"
F_REV, F_AR = "lifetime_revenue", "outstanding_ar"

# Fields copied from the old account into a CHOW replacement (identity, not billing).
CHOW_COPY_FIELDS = [F_NAME, F_ADDR, F_CITY, F_STATE, F_ZIP, "phone", "website", "facility_type",
                    "care_types", "care_offerings", "account_type", "type", "beds", "bed_count"]


@dataclass
class Match:
    account: dict
    tier: str  # A: street+zip exact, B: number+zip & fuzzy street, C: name+city, D: weak name
    signals: list = field(default_factory=list)

    @property
    def rank(self):
        return "ABCD".index(self.tier)


def location_key(loc):
    return f"{N.street(loc['address'])}|{N.zip5(loc['zip'])}"


def account_label(a):
    return f"{a.get(F_NAME)} ({a['id']})"


def find_parent_account(accounts, operator="bellhaven"):
    """The corporate Bellhaven account: a top-level account whose name says Bellhaven."""
    tops = [a for a in accounts if operator in N.clean(a.get(F_NAME)) and not a.get(F_PARENT)]
    if not tops:
        raise RuntimeError("No top-level Bellhaven parent account found in CRM.")
    # If the parent itself is duplicated, the one most facilities already point at wins.
    children = lambda p: sum(1 for a in accounts if str(a.get(F_PARENT)) == str(p["id"]))
    tops.sort(key=lambda p: (p.get(F_DUP) is not None, p.get(F_STATUS) != ACTIVE, -children(p)))
    return tops[0], tops[1:]


def match_location(loc, accounts):
    street, zip_, num = N.street(loc["address"]), N.zip5(loc["zip"]), N.street_number(loc["address"])
    core, city, state = N.name_core(loc["name"]), N.city(loc["city"]), N.state(loc["state"])
    matches = []
    for a in accounts:
        a_street, a_zip = N.street(a.get(F_ADDR)), N.zip5(a.get(F_ZIP))
        a_core = N.name_core(a.get(F_NAME))
        name_sim = N.similarity(core, a_core)
        same_city = N.city(a.get(F_CITY)) == city and N.state(a.get(F_STATE)) == state
        signals = []
        if street and street == a_street and zip_ == a_zip:
            tier = "A"
            signals.append("street address and ZIP match exactly")
        elif num and num == N.street_number(a.get(F_ADDR)) and zip_ == a_zip and N.similarity(street, a_street) >= 0.75:
            tier = "B"
            signals.append(f"street number + ZIP match, street similar ({N.similarity(street, a_street):.2f})")
        elif core and name_sim >= 0.9 and same_city:
            tier = "C"
            signals.append(f"name matches ({name_sim:.2f}) in same city, address differs")
        elif core and name_sim >= 0.85 and N.state(a.get(F_STATE)) == state:
            tier = "D"
            signals.append(f"similar name ({name_sim:.2f}) in same state only")
        else:
            continue
        signals.append(f"name similarity {name_sim:.2f} ('{loc['name']}' vs '{a.get(F_NAME)}')")
        matches.append(Match(a, tier, signals))
    matches.sort(key=lambda m: m.rank)
    return matches


def survivor_key(m, parent_id):
    """Which of several CRM copies of one facility to keep (lower sorts first)."""
    a = m.account
    return (
        a.get(F_DUP) is not None,  # already marked as someone's duplicate
        a.get(F_CHOW) is not None,  # superseded by a CHOW replacement
        money(a.get(F_REV)) <= 0,  # billing history lives here: keep it
        m.rank,
        a.get(F_STATUS) != ACTIVE,
        str(a.get(F_PARENT)) != str(parent_id),
        -sum(1 for v in a.values() if v not in (None, "", [])),
        str(a.get("created_at") or a["id"]),
    )


def comparison(loc, a):
    rows = [
        ("name", loc["name"], a.get(F_NAME), N.clean(loc["name"]) == N.clean(a.get(F_NAME))),
        ("address", loc["address"], a.get(F_ADDR), N.street(loc["address"]) == N.street(a.get(F_ADDR))),
        ("city", loc["city"], a.get(F_CITY), N.city(loc["city"]) == N.city(a.get(F_CITY))),
        ("state", loc["state"], a.get(F_STATE), N.state(loc["state"]) == N.state(a.get(F_STATE))),
        ("zip", loc["zip"], a.get(F_ZIP), N.zip5(loc["zip"]) == N.zip5(a.get(F_ZIP))),
        ("care offerings", ", ".join(loc.get("care_offerings", [])), a.get("care_types") or a.get("care_offerings"), True),
    ]
    return [{"field": f, "website": w, "crm": c, "match": ok} for f, w, c, ok in rows]


def related_row(role, a, parents):
    parent = a.get(F_PARENT)
    return {
        "role": role, "id": a["id"], "name": a.get(F_NAME),
        "address": f"{a.get(F_ADDR)}, {a.get(F_CITY)}, {a.get(F_STATE)} {a.get(F_ZIP)}",
        "parent": f"{parents.get(str(parent), '?')} ({parent})" if parent else "—",
        "status": a.get(F_STATUS),
        "billing": f"rev {money(a.get(F_REV)):,.2f} / AR {money(a.get(F_AR)):,.2f}",
    }


def field_fixes(loc, a):
    """Website is the source of truth for name and address of a current location."""
    fixes = {}
    if N.clean(loc["name"]) != N.clean(a.get(F_NAME)):
        fixes[F_NAME] = loc["name"]
    if N.street(loc["address"]) != N.street(a.get(F_ADDR)):
        fixes[F_ADDR] = loc["address"]
    if N.city(loc["city"]) != N.city(a.get(F_CITY)):
        fixes[F_CITY] = loc["city"]
    if N.state(loc["state"]) != N.state(a.get(F_STATE)):
        fixes[F_STATE] = loc["state"]
    if N.zip5(loc["zip"]) != N.zip5(a.get(F_ZIP)):
        fixes[F_ZIP] = loc["zip"]
    if a.get(F_STATUS) != ACTIVE:
        fixes[F_STATUS] = ACTIVE
    return fixes


def changes_rows(account_label_, before, after_fields):
    return [{"account": account_label_, "field": k, "before": before.get(k), "after": v} for k, v in after_fields.items()]


def build_proposals(locations, accounts, operator="bellhaven"):
    by_id = {str(a["id"]): a for a in accounts}
    parent, other_parents = find_parent_account(accounts, operator)
    parent_id = parent["id"]
    parent_names = {str(a["id"]): a.get(F_NAME) for a in accounts}
    corporate_ids = {str(parent_id)} | {str(p["id"]) for p in other_parents}
    # Facilities = every account that is not a corporate parent of other accounts.
    parent_ids = {str(a[F_PARENT]) for a in accounts if a.get(F_PARENT)} | corporate_ids
    facilities = [a for a in accounts if str(a["id"]) not in parent_ids]

    proposals, report = [], {"confirmed": [], "unmatched_locations": [], "ambiguous": []}
    claimed = set()  # account ids linked to some website location

    for loc in locations:
        key = location_key(loc)
        found = match_location(loc, facilities)
        matches = [m for m in found if m.tier in "ABC"]
        weak = [m for m in found if m.tier == "D"]

        if not matches:
            proposals.append(_create(loc, key, parent, weak, parent_names))
            report["unmatched_locations"].append(loc["name"])
            continue

        matches.sort(key=lambda m: survivor_key(m, parent_id))
        primary, dupes = matches[0], matches[1:]
        claimed.update(str(m.account["id"]) for m in matches)

        # If an account was already replaced via CHOW, the replacement is the live record.
        live = primary.account
        if live.get(F_CHOW) and str(live[F_CHOW]) in by_id:
            live = by_id[str(live[F_CHOW])]
            claimed.add(str(live["id"]))

        for d in dupes:
            if d.account.get(F_DUP) or d.account.get(F_CHOW) or str(d.account["id"]) == str(live["id"]):
                continue  # already resolved earlier
            proposals.append(_duplicate(loc, key, d, live, primary, parent_names))

        p = _fix_primary(loc, key, live, primary, parent, parent_names)
        if p:
            proposals.append(p)
        else:
            report["confirmed"].append(f"{loc['name']} -> {account_label(live)}")

    for a in facilities:
        if str(a.get(F_PARENT)) != str(parent_id) or str(a["id"]) in claimed:
            continue
        if a.get(F_DUP) or a.get(F_CHOW) or a.get(F_STATUS) in (INACTIVE, NEEDS_REVIEW):
            continue
        proposals.append(_not_on_website(a, parent, locations, parent_names))

    return proposals, report


def _sop_evidence(a):
    path = sop_path(a)
    rev, ar = money(a.get(F_REV)), money(a.get(F_AR))
    reason = (
        f"revenue history ({rev:,.2f}) AND outstanding AR ({ar:,.2f}) > 0: preserve old account, create new one, set CHOW pointer"
        if path == CHOW else
        ("no revenue history" if rev <= 0 else "no outstanding AR") + ": re-parent the existing account directly"
    )
    return {"lifetime_revenue": f"{rev:,.2f}", "outstanding_ar": f"{ar:,.2f}", "path": path, "reason": reason}


def _fix_primary(loc, key, a, match, parent, parents):
    fixes = field_fixes(loc, a)
    wrong_parent = str(a.get(F_PARENT)) != str(parent["id"])
    if not fixes and not wrong_parent:
        return None

    conf = {"A": "high", "B": "high", "C": "medium"}[match.tier]
    ev = {
        "comparison": comparison(loc, a),
        "reasoning": match.signals + [f"website listing: {loc.get('url', '')}"],
        "related": [related_row("matched account", a, parents)],
    }
    if wrong_parent:
        old_parent = parents.get(str(a.get(F_PARENT)), "none")
        sop = _sop_evidence(a)
        ev["sop"] = sop
        note = (f"Re-parented from {old_parent} ({a.get(F_PARENT)}) to {parent.get(F_NAME)} ({parent['id']}): "
                f"listed on Bellhaven website ({loc.get('url', '')}).")
        action = {"op": "reparent", "account_id": a["id"], "new_parent_id": parent["id"], "sop_path": sop["path"],
                  "expect": {F_PARENT: a.get(F_PARENT)}, "note": note}
        if sop["path"] == DIRECT:
            action["set"] = fixes
            ev["plan"] = [f"Set parent of {account_label(a)} to {parent.get(F_NAME)}" +
                          (f" and fix {', '.join(fixes)}" if fixes else "") + "."]
            ev["changes"] = changes_rows(account_label(a), a, {F_PARENT: parent["id"], **fixes})
        else:
            new = {k: a[k] for k in CHOW_COPY_FIELDS if a.get(k) not in (None, "")}
            new.update({F_NAME: loc["name"], F_ADDR: loc["address"], F_CITY: loc["city"], F_STATE: loc["state"],
                        F_ZIP: loc["zip"], F_STATUS: ACTIVE})
            action["new_account"] = new
            ev["plan"] = [
                f"Create a new account '{loc['name']}' under {parent.get(F_NAME)}.",
                f"Set chow_current_account on {account_label(a)} to the new account's id. No other change to the old account.",
            ]
            ev["changes"] = changes_rows("NEW account", {}, {**new, F_PARENT: parent["id"]}) + [
                {"account": account_label(a), "field": F_CHOW, "before": a.get(F_CHOW), "after": "<new account id>"}]
        ev["summary"] = (f"On the Bellhaven website but parented to {old_parent}. "
                         f"SOP path: {sop['path'].upper()}.")
        title = f"Move {a.get(F_NAME)} under Bellhaven" + (" (CHOW)" if sop["path"] == CHOW else "")
        if fixes and F_NAME in fixes:
            title += f"; name -> {fixes[F_NAME]}"
        return {"kind": "reparent", "subject": f"account:{a['id']}", "account_id": a["id"], "location_key": key,
                "title": title, "confidence": conf, "action": action, "evidence": ev}

    action = {"op": "update", "account_id": a["id"], "set": fixes,
              "expect": {k: a.get(k) for k in fixes},
              "note": f"Updated {', '.join(fixes)} to match Bellhaven website ({loc.get('url', '')})."}
    ev["plan"] = [f"Update {', '.join(fixes)} on {account_label(a)}."]
    ev["changes"] = changes_rows(account_label(a), a, fixes)
    only = set(fixes)
    kind = "rename" if only == {F_NAME} else "reactivate" if only == {F_STATUS} else "update"
    ev["summary"] = f"Correct parent; {', '.join(fixes)} out of date versus website."
    return {"kind": kind, "subject": f"account:{a['id']}", "account_id": a["id"], "location_key": key,
            "title": f"Update {a.get(F_NAME)}: " + ", ".join(f"{k} -> {v}" for k, v in fixes.items()),
            "confidence": conf, "action": action, "evidence": ev}


def _duplicate(loc, key, dup, survivor, primary, parents):
    a = dup.account
    note = f"Duplicate of {account_label(survivor)}: same facility ({loc['name']}, {loc['address']})."
    action = {"op": "update", "account_id": a["id"], "set": {F_DUP: survivor["id"], F_STATUS: INACTIVE},
              "expect": {F_DUP: a.get(F_DUP)}, "note": note}
    conf = "high" if dup.tier in "AB" and primary.tier in "AB" else "medium"
    reasons = dup.signals + [f"survivor chosen by: billing history, match strength, Active status, correct parent, completeness"]
    if money(a.get(F_REV)) > 0:
        conf = "medium"
        reasons.append("this copy also has billing history; it is kept intact apart from the duplicate flag")
    return {
        "kind": "duplicate", "subject": f"account:{a['id']}", "account_id": a["id"], "location_key": key,
        "title": f"Mark {a.get(F_NAME)} ({a['id']}) as duplicate of {survivor['id']}",
        "confidence": conf, "action": action,
        "evidence": {
            "summary": f"Two CRM accounts describe the same website location '{loc['name']}'.",
            "reasoning": reasons,
            "comparison": comparison(loc, a),
            "plan": [f"Set duplicate_of_account = {survivor['id']} and status = Inactive on {account_label(a)}."],
            "changes": changes_rows(account_label(a), a, {F_DUP: survivor["id"], F_STATUS: INACTIVE}),
            "related": [related_row("duplicate (loser)", a, parents), related_row("survivor", survivor, parents)],
        },
    }


def _create(loc, key, parent, weak, parents):
    fields = {F_NAME: loc["name"], F_ADDR: loc["address"], F_CITY: loc["city"], F_STATE: loc["state"],
              F_ZIP: loc["zip"], F_PARENT: parent["id"], F_STATUS: ACTIVE}
    reasons = ["no CRM account matches this address, or this name in this city"]
    if weak:
        reasons.append("weak name-only lookalikes exist (listed below): check they are not this facility")
    return {
        "kind": "create", "subject": f"location:{key}", "location_key": key,
        "title": f"Create account for {loc['name']} ({loc['city']}, {loc['state']})",
        "confidence": "medium" if weak else "high",
        "action": {"op": "create", "fields": fields, "note": f"Created from Bellhaven website listing ({loc.get('url', '')})."},
        "evidence": {
            "summary": "Listed on the Bellhaven website, no CRM account found.",
            "reasoning": reasons + [f"care offerings: {', '.join(loc.get('care_offerings', []))}"],
            "comparison": [{"field": k, "website": v, "crm": "—", "match": False} for k, v in fields.items() if k != F_PARENT],
            "plan": [f"Create '{loc['name']}' under {parent.get(F_NAME)} with status Active."],
            "changes": changes_rows("NEW account", {}, fields),
            "related": [related_row(f"weak lookalike ({w.tier})", w.account, parents) for w in weak],
        },
    }


def _not_on_website(a, parent, locations, parents):
    note = ("Not listed on the Bellhaven website as of this run. Possibly sold, closed or renamed; "
            "parent left unchanged until the new owner is confirmed.")
    near = sorted(locations, key=lambda l: -N.similarity(N.name_core(l["name"]), N.name_core(a.get(F_NAME))))[:1]
    reasons = ["account is parented to Bellhaven but no website location matches its address or name"]
    if near:
        reasons.append(f"closest website name: '{near[0]['name']}' ({near[0]['city']}, {near[0]['state']})")
    return {
        "kind": "not_on_website", "subject": f"account:{a['id']}", "account_id": a["id"], "location_key": None,
        "title": f"Flag {a.get(F_NAME)} ({a['id']}): under Bellhaven but not on website",
        "confidence": "medium",
        "action": {"op": "update", "account_id": a["id"], "set": {F_STATUS: NEEDS_REVIEW},
                   "expect": {F_PARENT: a.get(F_PARENT), F_STATUS: a.get(F_STATUS)}, "note": note},
        "evidence": {
            "summary": "Under the Bellhaven parent in CRM, but Bellhaven no longer lists it.",
            "reasoning": reasons,
            "plan": [f"Set status = Needs Review on {account_label(a)} and append a note. Parent unchanged."],
            "changes": changes_rows(account_label(a), a, {F_STATUS: NEEDS_REVIEW}),
            "related": [related_row("account", a, parents)],
        },
    }
