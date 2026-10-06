"""Link website locations to CRM accounts and turn every discrepancy into a proposal.

Matching rules (strongest first). Only A/B/C count as "the same facility":
  A  street address matches, and ZIP or city matches       (catches ZIP typos)
  B  street number + ZIP match, street name similar        ("Wilmington Pk" vs "Pike")
  C  same name + same city, address differs, BUT confirmed by the website phone
     or the website administrator being a contact on the account (PO Box billing addresses)
  L  lookalike: similar name without A/B/C. Never linked; shown as evidence only.
     (Union Square Senior Living, Amberly Manor in Colorado, Maplewood Senior Care Center.)

A proposal is a dict:
  kind       reparent | rename | update | annotate | duplicate | create |
             not_on_website | moved_away | parent_absorbed
  subject    stable identity used in the idempotency fingerprint
  action     exactly what apply.py will write (also part of the fingerprint)
  evidence   what the reviewer sees
  confidence high | medium | low
"""
import re
from collections import defaultdict
from dataclasses import dataclass, field

from . import normalize as N
from .apply import CHOW, DIRECT, money, same, sop_path

ACTIVE, INACTIVE, NEEDS_REVIEW = "Active", "Inactive", "Needs Review"

# CRM field names, in one place.
ID, F_NAME, F_PARENT, F_STATUS, F_NOTE = "account_id", "name", "parent_id", "status", "note"
F_STREET, F_CITY, F_STATE, F_ZIP = "billing_street", "billing_city", "billing_state", "billing_zip"
F_CARE, F_PHONE = "care_type", "phone"
F_DUP, F_CHOW = "duplicate_of_account", "chow_current_account"
F_REV, F_AR = "lifetime_revenue", "outstanding_ar"

# Website care offerings -> CRM care_type vocabulary.
CARE_TYPES = {
    "assisted living": "Assisted Living",
    "memory support": "Memory Care",
    "memory care": "Memory Care",
    "short-term rehabilitation & nursing": "Skilled Nursing",
    "skilled nursing": "Skilled Nursing",
    "independent living": "Independent Living",
}


def crm_care_types(loc):
    return [CARE_TYPES.get(o.lower(), o) for o in loc.get("care_offerings", [])]


def location_key(loc):
    return f"{N.street(loc['address'])}|{N.zip5(loc['zip'])}"


def label(a):
    return f"{a.get(F_NAME)} ({a[ID]})"


def operator_name(parent):
    return re.sub(r"\s*\(parent account\)\s*$", "", parent.get(F_NAME, ""), flags=re.I)


class Context:
    """Indexes over one CRM snapshot."""

    def __init__(self, accounts, contacts=(), about_text="", operator="bellhaven"):
        self.accounts = accounts
        self.by_id = {a[ID]: a for a in accounts}
        self.contacts = defaultdict(list)
        for c in contacts:
            self.contacts[c["account_id"]].append(c)
        referenced = {a[F_PARENT] for a in accounts if a.get(F_PARENT)}
        is_corporate = lambda a: a[ID] in referenced or "(parent account)" in a.get(F_NAME, "").lower()
        self.corporate = [a for a in accounts if is_corporate(a)]
        self.facilities = [a for a in accounts if not is_corporate(a)]
        self.children = defaultdict(list)
        for a in accounts:
            if a.get(F_PARENT):
                self.children[a[F_PARENT]].append(a)
        candidates = [a for a in self.corporate if operator in N.clean(a[F_NAME]) and not a.get(F_PARENT)]
        if not candidates:
            raise RuntimeError("No Bellhaven parent account found in CRM.")
        # If the corporate account were itself duplicated, the one with most children wins.
        candidates.sort(key=lambda p: (bool(p.get(F_DUP)), p.get(F_STATUS) != ACTIVE, -len(self.children[p[ID]])))
        self.parent = candidates[0]
        self.about = about_text or ""

    def parent_label(self, parent_id):
        return self.by_id[parent_id][F_NAME] if parent_id in self.by_id else "no parent"

    def about_mentions(self, parent_id):
        """The About-page sentence naming this operator, if any (acquisition evidence)."""
        if parent_id not in self.by_id:
            return None
        name = operator_name(self.by_id[parent_id]).lower()
        words = [w for w in N.clean(name).split() if w not in {"care", "group", "communities", "healthcare", "health"}]
        for sentence in re.split(r"(?<=[.!?])\s+", self.about):
            if words and all(w in N.clean(sentence) for w in words):
                return sentence.strip()
        return None


@dataclass
class Match:
    account: dict
    tier: str
    signals: list = field(default_factory=list)
    corroboration: int = 0

    @property
    def rank(self):
        return "ABCL".index(self.tier)


def compare(loc, a, ctx):
    """Classify one (website location, CRM account) pair, or None if unrelated."""
    s_loc, s_acc = N.street(loc["address"]), N.street(a.get(F_STREET))
    zip_eq = N.zip5(loc["zip"]) == N.zip5(a.get(F_ZIP)) != ""
    city_eq = N.city(loc["city"]) == N.city(a.get(F_CITY)) and N.state(loc["state"]) == N.state(a.get(F_STATE))
    street_eq = bool(s_loc) and s_loc == s_acc
    num = N.street_number(loc["address"])
    num_eq = bool(num) and num == N.street_number(a.get(F_STREET))
    street_sim = N.similarity(s_loc, s_acc)
    name_eq = N.clean(loc["name"]) == N.clean(a.get(F_NAME))
    core_loc, core_acc = N.name_core(loc["name"]), N.name_core(a.get(F_NAME))
    core_sim = N.similarity(core_loc, core_acc) if core_loc and core_acc else 0.0
    phone_eq = bool(N.phone(loc.get("phone"))) and N.phone(loc.get("phone")) == N.phone(a.get(F_PHONE))
    admin = N.person(loc.get("administrator"))
    admin_eq = bool(admin) and any(N.person(c["name"]) == admin for c in ctx.contacts[a[ID]])
    po_box = N.is_po_box(a.get(F_STREET))

    if street_eq and zip_eq:
        tier, signals = "A", ["street address and ZIP match"]
    elif street_eq and city_eq:
        tier, signals = "A", [f"street address and city match; ZIP differs ({a.get(F_ZIP)} vs {loc['zip']})"]
    elif num_eq and zip_eq and street_sim >= 0.75:
        tier, signals = "B", [f"street number + ZIP match, street name similar ('{a.get(F_STREET)}' vs '{loc['address']}')"]
    elif city_eq and (name_eq or core_sim >= 0.9) and (phone_eq or admin_eq):
        why = "CRM street is a PO Box" if po_box else f"CRM street '{a.get(F_STREET)}' differs"
        tier, signals = "C", [f"same name and city; {why}; identity confirmed by phone/administrator"]
    elif name_eq or core_sim >= 0.85:
        tier = "L"
        signals = [f"similar name ({core_sim:.2f}) but NOT the same facility:"]
        if not city_eq:
            signals.append(f"different location ({a.get(F_CITY)}, {a.get(F_STATE)} vs {loc['city']}, {loc['state']})")
        else:
            signals.append(f"different street address ('{a.get(F_STREET)}' vs '{loc['address']}')")
        contact_names = [c["name"] for c in ctx.contacts[a[ID]]]
        if contact_names and not admin_eq:
            signals.append(f"administrator differs (CRM contacts {contact_names} vs website '{loc.get('administrator')}')")
        if not phone_eq:
            signals.append("phone differs")
        return Match(a, tier, signals, 0)
    else:
        return None

    corroboration = 0
    if phone_eq:
        corroboration += 1
        signals.append(f"phone matches website ({loc.get('phone')})")
    if admin_eq:
        corroboration += 1
        signals.append(f"website administrator '{loc.get('administrator')}' is a contact on this account")
    if name_eq:
        corroboration += 1
        signals.append("name identical to website")
    elif core_sim:
        signals.append(f"name similarity {core_sim:.2f} ('{a.get(F_NAME)}' vs '{loc['name']}')")
    if (a.get(F_STREET) or "").strip().lower() == loc["address"].strip().lower():
        corroboration += 1
    return Match(a, tier, signals, corroboration)


def survivor_key(m, ctx):
    """Which CRM copy of one facility to keep (lowest sorts first)."""
    a = m.account
    return (
        bool(a.get(F_DUP)),  # already marked as someone's duplicate
        bool(a.get(F_CHOW)),  # already superseded through a CHOW
        money(a.get(F_REV)) <= 0,  # billing history lives here: keep it
        m.rank,
        -m.corroboration,
        a.get(F_PARENT) != ctx.parent[ID],
        a.get(F_STATUS) != ACTIVE,
        -len(ctx.contacts[a[ID]]),
        a[ID],
    )


def comparison(loc, a):
    rows = [
        ("name", loc["name"], a.get(F_NAME), N.clean(loc["name"]) == N.clean(a.get(F_NAME))),
        ("street", loc["address"], a.get(F_STREET), N.street(loc["address"]) == N.street(a.get(F_STREET))),
        ("city", loc["city"], a.get(F_CITY), N.city(loc["city"]) == N.city(a.get(F_CITY))),
        ("state", loc["state"], a.get(F_STATE), N.state(loc["state"]) == N.state(a.get(F_STATE))),
        ("zip", loc["zip"], a.get(F_ZIP), N.zip5(loc["zip"]) == N.zip5(a.get(F_ZIP))),
        ("care", ", ".join(loc.get("care_offerings", [])), a.get(F_CARE), a.get(F_CARE) in crm_care_types(loc)),
        ("phone", loc.get("phone"), a.get(F_PHONE), N.phone(loc.get("phone")) == N.phone(a.get(F_PHONE))),
    ]
    return [{"field": f, "website": w, "crm": c, "match": ok} for f, w, c, ok in rows]


def related_row(role, a, ctx):
    contacts = ", ".join(f"{c['name']} ({c['title']})" for c in ctx.contacts[a[ID]]) or "—"
    return {
        "role": role, "id": a[ID], "name": a.get(F_NAME),
        "address": f"{a.get(F_STREET)}, {a.get(F_CITY)}, {a.get(F_STATE)} {a.get(F_ZIP)}",
        "parent": ctx.parent_label(a.get(F_PARENT)) if a.get(F_PARENT) else "—",
        "status": a.get(F_STATUS),
        "billing": f"rev {money(a.get(F_REV)):,.0f} / AR {money(a.get(F_AR)):,.0f}",
        "contacts": contacts,
    }


def changes_rows(who, before, after_fields):
    return [{"account": who, "field": k, "before": before.get(k), "after": v} for k, v in after_fields.items()]


def field_fixes(loc, a):
    """Website is the source of truth for a current location's identity fields."""
    fixes = {}
    if N.clean(loc["name"]) != N.clean(a.get(F_NAME)):
        fixes[F_NAME] = loc["name"]
    if N.street(loc["address"]) != N.street(a.get(F_STREET)) and not N.is_po_box(a.get(F_STREET)):
        fixes[F_STREET] = loc["address"]
    if N.city(loc["city"]) != N.city(a.get(F_CITY)):
        fixes[F_CITY] = loc["city"]
    if N.state(loc["state"]) != N.state(a.get(F_STATE)):
        fixes[F_STATE] = loc["state"]
    if N.zip5(loc["zip"]) != N.zip5(a.get(F_ZIP)):
        fixes[F_ZIP] = loc["zip"]
    care = crm_care_types(loc)
    if care and a.get(F_CARE) not in care:
        fixes[F_CARE] = care[0]
    if a.get(F_STATUS) != ACTIVE:
        fixes[F_STATUS] = ACTIVE
    return fixes


def website_fields(loc):
    care = crm_care_types(loc)
    return {F_NAME: loc["name"], F_STREET: loc["address"], F_CITY: loc["city"], F_STATE: loc["state"],
            F_ZIP: loc["zip"], F_CARE: care[0] if care else "", F_PHONE: loc.get("phone", ""), F_STATUS: ACTIVE}


def sop_evidence(a):
    path = sop_path(a)
    rev, ar = money(a.get(F_REV)), money(a.get(F_AR))
    if path == CHOW:
        reason = f"revenue history ({rev:,.0f}) AND outstanding AR ({ar:,.0f}) > 0: old account must stay exactly as it is"
    else:
        reason = ("no revenue history" if rev <= 0 else f"revenue {rev:,.0f} but no outstanding AR") + ": re-parent the existing account"
    return {"lifetime_revenue": f"{rev:,.2f}", "outstanding_ar": f"{ar:,.2f}", "path": path, "reason": reason}


# --------------------------------------------------------------------------- build


def build_proposals(locations, accounts, contacts=(), about_text=""):
    ctx = Context(accounts, contacts, about_text)
    proposals = []
    report = {"confirmed": [], "created": [], "lookalikes_rejected": [], "bellhaven_parent": label(ctx.parent)}
    claimed = set()  # account ids that belong to some website location
    leaving = set()  # account ids this run proposes to deactivate or move off their parent

    for loc in locations:
        key = location_key(loc)
        found = [m for m in (compare(loc, a, ctx) for a in ctx.facilities) if m]
        matches = [m for m in found if m.tier != "L"]
        lookalikes = [m for m in found if m.tier == "L"]
        report["lookalikes_rejected"] += [f"{loc['name']} != {label(m.account)}" for m in lookalikes]

        if not matches:
            proposals.append(_create(loc, key, ctx, lookalikes))
            report["created"].append(loc["name"])
            continue

        matches.sort(key=lambda m: survivor_key(m, ctx))
        primary, others = matches[0], matches[1:]
        claimed.update(m.account[ID] for m in matches)

        live, via_chow = primary.account, None
        if live.get(F_CHOW) and live[F_CHOW] in ctx.by_id:  # follow an earlier CHOW to the current record
            via_chow, live = live, ctx.by_id[live[F_CHOW]]
            claimed.add(live[ID])

        for d in others:
            a = d.account
            if a.get(F_DUP) or a.get(F_CHOW) or a[ID] == live[ID]:
                continue  # already resolved on an earlier run
            proposals.append(_duplicate(loc, key, d, live, ctx))
            leaving.add(a[ID])

        p = _fix_primary(loc, key, live, primary, ctx, lookalikes, via_chow)
        if p:
            proposals.append(p)
            if p["kind"] == "reparent" and p["action"]["sop_path"] == DIRECT:
                leaving.add(live[ID])
        else:
            report["confirmed"].append(f"{loc['name']} -> {label(live)}")

    # Bellhaven children the website no longer lists.
    for a in ctx.children[ctx.parent[ID]]:
        if a[ID] in claimed or a.get(F_DUP) or a.get(F_CHOW) or a.get(F_STATUS) in (INACTIVE, NEEDS_REVIEW):
            continue
        p = _not_on_website(a, ctx, locations)
        proposals.append(p)

    # Operators Bellhaven absorbed whose corporate account no longer has any live facility.
    for corp in ctx.corporate:
        if corp[ID] == ctx.parent[ID] or corp.get(F_STATUS) != ACTIVE or not ctx.children[corp[ID]]:
            continue
        sentence = ctx.about_mentions(corp[ID])
        remaining = [c for c in ctx.children[corp[ID]]
                     if c.get(F_STATUS) == ACTIVE and not c.get(F_DUP) and c[ID] not in leaving]
        if sentence and not remaining:
            proposals.append(_parent_absorbed(corp, ctx, sentence))

    return proposals, report


# --------------------------------------------------------------------------- proposal builders


def _confidence(match):
    return {"A": "high", "B": "high", "C": "medium"}[match.tier]


def _fix_primary(loc, key, a, match, ctx, lookalikes, via_chow):
    parent = ctx.parent
    fixes = field_fixes(loc, a)
    wrong_parent = a.get(F_PARENT) != parent[ID]
    po_box_note = None
    if N.is_po_box(a.get(F_STREET)) and loc["address"] not in (a.get(F_NOTE) or ""):
        po_box_note = (f"Physical address per Bellhaven website: {loc['address']}, {loc['city']}, {loc['state']} "
                       f"{loc['zip']}. billing_street left as '{a.get(F_STREET)}' (a PO Box is a valid billing address).")
    if not fixes and not wrong_parent and not po_box_note:
        return None

    reasoning = list(match.signals)
    if via_chow:
        reasoning.insert(0, f"{label(via_chow)} was already replaced via CHOW by {label(a)}; checking the replacement")
    reasoning += [f"not linked (lookalike): {label(m.account)}: {'; '.join(m.signals[1:])}" for m in lookalikes]
    reasoning.append(f"website listing: {loc.get('url', '')}")
    ev = {"comparison": comparison(loc, a), "reasoning": reasoning,
          "related": [related_row("matched account", a, ctx)] + [related_row("lookalike (not linked)", m.account, ctx) for m in lookalikes]}
    conf = _confidence(match)

    if wrong_parent:
        old = a.get(F_PARENT)
        acquisition = ctx.about_mentions(old) if old else None
        if old and not acquisition:
            conf = "medium"
            reasoning.append(f"caution: Bellhaven's About page does not mention acquiring from {ctx.parent_label(old)}")
        elif acquisition:
            reasoning.append(f"About page: \"{acquisition}\"")
        sop = sop_evidence(a)
        ev["sop"] = sop
        note = (f"Moved from {ctx.parent_label(old)} to {parent[F_NAME]}: listed on the Bellhaven website "
                f"({loc.get('url', '')}).")
        action = {"op": "reparent", "account_id": a[ID], "new_parent_id": parent[ID], "sop_path": sop["path"],
                  "expect": {F_PARENT: old or ""}, "note": note}
        if sop["path"] == DIRECT:
            action["set"] = fixes
            ev["plan"] = [f"Set parent of {label(a)} to {parent[F_NAME]}" + (f" and update {', '.join(fixes)}" if fixes else "") + "."]
            ev["changes"] = changes_rows(label(a), a, {F_PARENT: parent[ID], **fixes})
        else:
            new = website_fields(loc)
            action["new_account"] = new
            ev["plan"] = [
                f"Create a new account '{loc['name']}' under {parent[F_NAME]} (website data).",
                f"Set chow_current_account on {label(a)} to the new account. Nothing else on the old account changes "
                f"(parent stays {ctx.parent_label(old)}, billing history preserved).",
            ]
            ev["changes"] = changes_rows("NEW account", {}, {**new, F_PARENT: parent[ID]}) + [
                {"account": label(a), "field": F_CHOW, "before": a.get(F_CHOW), "after": "<new account id>"}]
        ev["summary"] = f"Listed on the Bellhaven website but parented to {ctx.parent_label(old)}. SOP path: {sop['path'].upper()}."
        title = f"Move {a[F_NAME]} under Bellhaven" + (" via CHOW (new account)" if sop["path"] == CHOW else "")
        if F_NAME in fixes:
            title += f"; rename to {fixes[F_NAME]}"
        return {"kind": "reparent", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": key,
                "title": title, "confidence": conf, "action": action, "evidence": ev}

    note = f"Updated {', '.join(fixes)} to match the Bellhaven website ({loc.get('url', '')})." if fixes else None
    if po_box_note:
        note = f"{note} {po_box_note}" if note else po_box_note
    action = {"op": "update", "account_id": a[ID], "set": fixes, "expect": {k: a.get(k) or "" for k in fixes}, "note": note}
    ev["plan"] = ([f"Update {', '.join(fixes)} on {label(a)}."] if fixes else []) + (
        [f"Append a note with the physical address; leave the PO Box billing address."] if po_box_note else [])
    ev["changes"] = changes_rows(label(a), a, fixes) + (
        [{"account": label(a), "field": "note (append)", "before": a.get(F_NOTE), "after": po_box_note}] if po_box_note else [])
    kind = ("annotate" if not fixes else "rename" if set(fixes) == {F_NAME} else "update")
    ev["summary"] = "Correct parent; " + (f"{', '.join(fixes)} out of date versus the website." if fixes else "billing address is a PO Box.")
    title = (f"Update {a[F_NAME]}: " + "; ".join(f"{k} -> {v}" for k, v in fixes.items())) if fixes else f"Note physical address on {a[F_NAME]}"
    return {"kind": kind, "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": key,
            "title": title, "confidence": conf, "action": action, "evidence": ev}


def _duplicate(loc, key, dup, survivor, ctx):
    a = dup.account
    moved = [c["contact_id"] for c in ctx.contacts[a[ID]]]
    note = f"Duplicate of {label(survivor)}: same facility as Bellhaven website listing '{loc['name']}' ({loc['address']})."
    action = {"op": "update", "account_id": a[ID], "set": {F_DUP: survivor[ID], F_STATUS: INACTIVE},
              "expect": {F_DUP: a.get(F_DUP) or ""}, "note": note,
              "move_contacts": moved, "move_contacts_to": survivor[ID]}
    conf = "high" if dup.tier in "AB" else "medium"
    reasons = dup.signals + ["kept copy chosen by: billing history, match strength, phone/administrator confirmation, "
                             "already under Bellhaven, Active, contacts, then lowest id"]
    if money(a.get(F_REV)) > 0:
        conf = "medium"
        reasons.append("this copy has billing history too; only the duplicate flag and status change")
    plan = [f"Set duplicate_of_account = {survivor[ID]} and status = Inactive on {label(a)}; append a note."]
    if moved:
        plan.append(f"Move {len(moved)} contact(s) to {label(survivor)} so reps keep them.")
    return {
        "kind": "duplicate", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": key,
        "title": f"Mark {a[F_NAME]} ({ctx.parent_label(a.get(F_PARENT))}) as duplicate of {survivor[F_NAME]}",
        "confidence": conf, "action": action,
        "evidence": {
            "summary": f"Several CRM accounts describe the same website location '{loc['name']}'.",
            "reasoning": reasons,
            "comparison": comparison(loc, a),
            "plan": plan,
            "changes": changes_rows(label(a), a, {F_DUP: survivor[ID], F_STATUS: INACTIVE}),
            "related": [related_row("duplicate (to deactivate)", a, ctx), related_row("kept", survivor, ctx)],
        },
    }


def _create(loc, key, ctx, lookalikes):
    fields = {**website_fields(loc), F_PARENT: ctx.parent[ID]}
    reasons = ["no CRM account matches this street address, and no same-name account in this city is confirmed by phone or administrator"]
    if "directory" not in loc.get("found_on", ["directory"]):
        reasons.append(f"found only on {', '.join(loc['found_on'])} (not in the paginated directory)")
    for m in lookalikes:
        reasons.append(f"lookalike NOT linked: {label(m.account)} under {ctx.parent_label(m.account.get(F_PARENT))}: {'; '.join(m.signals[1:])}")
    same_city = any(N.city(m.account.get(F_CITY)) == N.city(loc["city"]) for m in lookalikes)
    return {
        "kind": "create", "subject": f"location:{key}", "location_key": key,
        "title": f"Create account for {loc['name']} ({loc['city']}, {loc['state']})",
        "confidence": "medium" if same_city else "high",
        "action": {"op": "create", "fields": fields, "note": f"Created from Bellhaven website listing ({loc.get('url', '')})."},
        "evidence": {
            "summary": "Listed on the Bellhaven website; no CRM account for this facility.",
            "reasoning": reasons,
            "comparison": [{"field": k, "website": v, "crm": "—", "match": False} for k, v in fields.items() if k != F_PARENT],
            "plan": [f"Create '{loc['name']}' under {ctx.parent[F_NAME]}, status Active."],
            "changes": changes_rows("NEW account", {}, fields),
            "related": [related_row("lookalike (not linked)", m.account, ctx) for m in lookalikes],
        },
    }


def _not_on_website(a, ctx, locations):
    # Did another operator take it over? Look for an account at the same address under a different parent.
    taker = None
    for other in ctx.facilities:
        if other[ID] == a[ID] or other.get(F_PARENT) in ("", None, ctx.parent[ID]) or other.get(F_DUP):
            continue
        if N.street(other.get(F_STREET)) == N.street(a.get(F_STREET)) and N.zip5(other.get(F_ZIP)) == N.zip5(a.get(F_ZIP)):
            taker = other
            break
    if taker:
        return _moved_away(a, taker, ctx)

    near = sorted(locations, key=lambda l: -N.similarity(N.name_core(l["name"]), N.name_core(a.get(F_NAME))))[:1]
    reasons = [f"parented to Bellhaven, but no website location matches its address ({a.get(F_STREET)}, {a.get(F_CITY)}) or name",
               "no other operator's account exists at this address, so the new owner (if any) is unknown"]
    if near:
        reasons.append(f"closest website name: '{near[0]['name']}' ({near[0]['city']}, {near[0]['state']})")
    note = ("Not listed on the Bellhaven website (communities directory or homepage). Possibly sold, closed or renamed; "
            "parent left unchanged until a rep confirms the current owner.")
    return {
        "kind": "not_on_website", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": None,
        "title": f"Flag {a[F_NAME]}: under Bellhaven but not on the website",
        "confidence": "medium",
        "action": {"op": "update", "account_id": a[ID], "set": {F_STATUS: NEEDS_REVIEW},
                   "expect": {F_PARENT: a.get(F_PARENT), F_STATUS: a.get(F_STATUS)}, "note": note},
        "evidence": {
            "summary": "Under the Bellhaven parent in CRM, but Bellhaven no longer lists it.",
            "reasoning": reasons,
            "plan": [f"Set status = Needs Review on {label(a)} and append a note. Parent unchanged."],
            "changes": changes_rows(label(a), a, {F_STATUS: NEEDS_REVIEW}),
            "related": [related_row("account", a, ctx)],
        },
    }


def _moved_away(a, taker, ctx):
    new_parent = taker[F_PARENT]
    sop = sop_evidence(a)
    note = (f"No longer a Bellhaven community (not on the website); {ctx.parent_label(new_parent)} has an account "
            f"at the same address: {label(taker)}.")
    action = {"op": "reparent", "account_id": a[ID], "new_parent_id": new_parent, "sop_path": sop["path"],
              "expect": {F_PARENT: a.get(F_PARENT)}, "note": note}
    if sop["path"] == CHOW:
        action["chow_target_id"] = taker[ID]
        plan = [f"Set chow_current_account on {label(a)} to {label(taker)}, the existing account under "
                f"{ctx.parent_label(new_parent)}. Nothing else on the old account changes.",
                "No new account is created: the new owner's account already exists, and creating another would be a duplicate."]
        changes = [{"account": label(a), "field": F_CHOW, "before": a.get(F_CHOW), "after": taker[ID]}]
    else:
        action["set"] = {}
        plan = [f"Re-parent {label(a)} to {ctx.parent_label(new_parent)}; note the existing account {label(taker)} "
                "at the same address for duplicate review."]
        changes = changes_rows(label(a), a, {F_PARENT: new_parent})
    return {
        "kind": "moved_away", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": None,
        "title": f"{a[F_NAME]} now belongs to {operator_name(ctx.by_id[new_parent])}" + (" (CHOW pointer)" if sop["path"] == CHOW else ""),
        "confidence": "high",
        "action": action,
        "evidence": {
            "summary": f"Not on the Bellhaven website; {ctx.parent_label(new_parent)} has an account at the same street address and ZIP.",
            "reasoning": [f"same address: {a.get(F_STREET)}, {a.get(F_CITY)} {a.get(F_ZIP)}",
                          f"{label(taker)} is under {ctx.parent_label(new_parent)}"],
            "sop": sop,
            "plan": plan,
            "changes": changes,
            "related": [related_row("Bellhaven account (old owner)", a, ctx), related_row("current owner's account", taker, ctx)],
        },
    }


def _parent_absorbed(corp, ctx, sentence):
    note = (f"Operator absorbed by {ctx.parent[F_NAME]}: \"{sentence}\" (Bellhaven website /about). "
            "No active facilities remain under this parent; route outreach through Bellhaven corporate.")
    return {
        "kind": "parent_absorbed", "subject": f"account:{corp[ID]}", "account_id": corp[ID], "location_key": None,
        "title": f"Deactivate {corp[F_NAME]}: acquired by Bellhaven, no facilities left",
        "confidence": "medium",
        "action": {"op": "update", "account_id": corp[ID], "set": {F_STATUS: INACTIVE},
                   "expect": {F_STATUS: corp.get(F_STATUS)}, "note": note},
        "evidence": {
            "summary": "Corporate account of an operator Bellhaven says it acquired; every facility under it is now a Bellhaven community.",
            "reasoning": [f"About page: \"{sentence}\"",
                          "after this run's proposals, no Active non-duplicate facility remains under this parent"],
            "plan": [f"Set status = Inactive on {label(corp)}; append a note pointing reps to Bellhaven corporate."],
            "changes": changes_rows(label(corp), corp, {F_STATUS: INACTIVE}),
            "related": [related_row("former child", c, ctx) for c in ctx.children[corp[ID]]],
        },
    }
