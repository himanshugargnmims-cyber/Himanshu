"""Link website locations to CRM accounts and turn every discrepancy into a proposal.

Matching rules (strongest first). A/B/C/D link a location to an account:
  A  street address matches, and ZIP or city matches       (catches ZIP typos)
  B  same street number + ZIP, same street type and direction, street name
     spelled slightly differently                          (abbreviation variants)
  C  same name + same city, address differs, confirmed by the website phone
     or by the website administrator being a contact on the account (PO Box billing)
  D  same name + same city, address differs, unconfirmed, but the account is
     already under Bellhaven: linked at LOW confidence so the reviewer checks it
  L  lookalike: similar name, none of the above. Never linked; shown as evidence.
     (Union Square Senior Living, Amberly Manor in Colorado, Maplewood Senior Care Center.)

Proposal kinds:
  reparent        on the website but under another parent -> billing SOP (direct or CHOW)
  rename/update   identity fields out of date versus the website
  annotate        note only (e.g. physical address for a PO Box billing account)
  duplicate       extra copy of a facility -> duplicate_of_account + Inactive
  chow_duplicate  extra copy under the old owner WITH revenue and AR -> SOP: keep it, point it at the live one
  create          on the website, no CRM account
  not_on_website  under Bellhaven, not listed, new owner unknown -> Needs Review
  moved_away      under Bellhaven, not listed, another operator has an account at the address
  operator_note   parent company the About page says Bellhaven acquired from -> note quoting it
"""
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from . import normalize as N
from .apply import CHOW, DIRECT, money, sop_path

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
CONFIDENCE = {"A": "high", "B": "high", "C": "medium", "D": "low"}


def crm_care_types(loc):
    return [CARE_TYPES.get(o.lower(), o) for o in loc.get("care_offerings", [])]


def location_key(loc):
    return f"{N.street(loc['address'])}|{N.zip5(loc['zip'])}"


def label(a):
    return f"{a.get(F_NAME)} ({a[ID]})"


def operator_name(parent):
    return re.sub(r"\s*\(parent account\)\s*$", "", parent.get(F_NAME, ""), flags=re.I)


def has_billing_and_ar(a):
    return sop_path(a) == CHOW


class Context:
    """Indexes over one CRM snapshot."""

    def __init__(self, accounts, contacts=(), about_text="", operator="bellhaven"):
        self.by_id = {a[ID]: a for a in accounts}
        self.contacts = defaultdict(list)
        for c in contacts:
            self.contacts[c["account_id"]].append(c)
        referenced = {a[F_PARENT] for a in accounts if a.get(F_PARENT)}
        # A corporate account is named as one, or is a parent with no street address of its own.
        # (A facility mistakenly used as someone's parent stays a facility.)
        self.is_corporate = lambda a: ("(parent account)" in a.get(F_NAME, "").lower()
                                       or (a[ID] in referenced and not (a.get(F_STREET) or "").strip()))
        self.corporate = [a for a in accounts if self.is_corporate(a)]
        self.facilities = [a for a in accounts if not self.is_corporate(a)]
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
        generic = {"care", "group", "communities", "healthcare", "health", "partners", "senior", "living"}
        words = [w for w in N.clean(operator_name(self.by_id[parent_id])).split() if w not in generic]
        for sentence in re.split(r"(?<=[.!?])\s+", self.about):
            if words and all(w in N.clean(sentence).split() for w in words):
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
        return "ABCDL".index(self.tier)


def compare(loc, a, ctx):
    """Classify one (website location, CRM account) pair, or None if unrelated."""
    s_loc, s_acc = N.street(loc["address"]), N.street(a.get(F_STREET))
    zip_eq = N.zip5(loc["zip"]) == N.zip5(a.get(F_ZIP)) != ""
    city_eq = N.city(loc["city"]) == N.city(a.get(F_CITY)) and N.state(loc["state"]) == N.state(a.get(F_STATE))
    street_eq = bool(s_loc) and s_loc == s_acc
    name_eq = N.clean(loc["name"]) == N.clean(a.get(F_NAME))
    core_loc, core_acc = N.name_core(loc["name"]), N.name_core(a.get(F_NAME))
    core_sim = N.similarity(core_loc, core_acc) if core_loc and core_acc else 0.0
    phone_eq = bool(N.phone(loc.get("phone"))) and N.phone(loc.get("phone")) == N.phone(a.get(F_PHONE))
    admin = N.person(loc.get("administrator"))
    admin_eq = bool(admin) and any(N.person(c["name"]) == admin for c in ctx.contacts[a[ID]])
    same_name = name_eq or core_sim >= 0.9

    if street_eq and zip_eq:
        tier, signals = "A", ["street address and ZIP match"]
    elif street_eq and city_eq:
        tier, signals = "A", [f"street address and city match; ZIP differs ({a.get(F_ZIP)} vs {loc['zip']})"]
    elif zip_eq and N.street_variant(s_loc, s_acc):
        tier, signals = "B", [f"same street number, type and direction + ZIP; spelling differs ('{a.get(F_STREET)}' vs '{loc['address']}')"]
    elif city_eq and same_name and (phone_eq or admin_eq):
        why = "CRM street is a PO Box" if N.is_po_box(a.get(F_STREET)) else f"CRM street '{a.get(F_STREET)}' differs"
        tier, signals = "C", [f"same name and city; {why}; identity confirmed by phone/administrator"]
    elif city_eq and same_name and a.get(F_PARENT) == ctx.parent[ID]:
        tier, signals = "D", [f"same name and city, already under Bellhaven, but street differs ('{a.get(F_STREET)}' vs "
                              f"'{loc['address']}') and nothing confirms it: verify before approving"]
    elif name_eq or core_sim >= 0.85:
        signals = [f"similar name ({core_sim:.2f}) but NOT the same facility:"]
        if not city_eq:
            signals.append(f"different location ({a.get(F_CITY)}, {a.get(F_STATE)} vs {loc['city']}, {loc['state']})")
        else:
            signals.append(f"different street address ('{a.get(F_STREET)}' vs '{loc['address']}')")
        names = [c["name"] for c in ctx.contacts[a[ID]]]
        if names and not admin_eq:
            signals.append(f"administrator differs (CRM contacts {names} vs website '{loc.get('administrator')}')")
        if not phone_eq:
            signals.append("phone differs")
        return Match(a, "L", signals, 0)
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
    return Match(a, tier, signals, corroboration)


def survivor_key(m, ctx, pointed_at):
    """Which CRM copy of one facility to keep (lowest sorts first)."""
    a = m.account
    return (
        bool(a.get(F_DUP)),  # already marked as someone's duplicate
        bool(a.get(F_CHOW)),  # already superseded through a CHOW
        a[ID] not in pointed_at,  # an earlier decision already made it the survivor: stay consistent
        m.rank,
        a.get(F_PARENT) != ctx.parent[ID],  # already the Bellhaven record
        -m.corroboration,
        a.get(F_STATUS) != ACTIVE,
        money(a.get(F_REV)) <= 0,
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


def build_proposals(locations, accounts, contacts=(), about_text="", site_complete=True):
    """site_complete=False (fewer locations than the website claims) suppresses 'gone from website' proposals."""
    ctx = Context(accounts, contacts, about_text)
    proposals = []
    report = {"confirmed": [], "created": [], "lookalikes_rejected": [], "colocated": [],
              "bellhaven_parent": label(ctx.parent), "site_complete": site_complete}
    claimed = set()  # account ids that belong to some website location
    assigned = {}  # account id -> location name it is the live record for
    shared = Counter(location_key(l) for l in locations)
    pointed_at = {a[F_DUP] for a in accounts if a.get(F_DUP)} | {a[F_CHOW] for a in accounts if a.get(F_CHOW)}

    for loc in locations:
        key = location_key(loc)
        found = [m for m in (compare(loc, a, ctx) for a in ctx.facilities) if m]
        lookalikes = [m for m in found if m.tier == "L"]
        matches = [m for m in found if m.tier != "L" and m.account[ID] not in assigned]
        if shared[key] > 1:  # several communities share one street address: only the name can tell them apart
            matches = [m for m in matches if N.similarity(N.name_core(loc["name"]), N.name_core(m.account[F_NAME])) >= 0.8]
            report["colocated"].append(loc["name"])
        report["lookalikes_rejected"] += [f"{loc['name']} != {label(m.account)}" for m in lookalikes]

        if not matches:
            proposals.append(_create(loc, key, ctx, lookalikes))
            report["created"].append(loc["name"])
            continue

        matches.sort(key=lambda m: survivor_key(m, ctx, pointed_at))
        primary, others = matches[0], matches[1:]
        claimed.update(m.account[ID] for m in matches)

        live, via_chow = primary.account, None
        if live.get(F_CHOW) and live[F_CHOW] in ctx.by_id:  # follow an earlier CHOW to the current record
            via_chow, live = live, ctx.by_id[live[F_CHOW]]
            claimed.add(live[ID])
        assigned[live[ID]] = loc["name"]

        if shared[key] == 1:  # never dedupe across co-located communities automatically
            for d in others:
                a = d.account
                if a.get(F_DUP) or a.get(F_CHOW) or a[ID] == live[ID]:
                    continue  # already resolved on an earlier run
                assigned[a[ID]] = loc["name"]
                if has_billing_and_ar(a) and a.get(F_PARENT) != live.get(F_PARENT):
                    proposals.append(_chow_duplicate(loc, key, d, live, ctx))
                else:
                    proposals.append(_duplicate(loc, key, d, live, ctx))

        p = _fix_primary(loc, key, live, primary, ctx, lookalikes, via_chow)
        if p:
            proposals.append(p)
        else:
            report["confirmed"].append(f"{loc['name']} -> {label(live)}")

    # Bellhaven children the website no longer lists. Only trusted when the scrape is complete.
    if site_complete:
        for a in ctx.children[ctx.parent[ID]]:
            if ctx.is_corporate(a) or a[ID] in claimed or a.get(F_DUP) or a.get(F_CHOW) or a.get(F_STATUS) in (INACTIVE, NEEDS_REVIEW):
                continue
            proposals.append(_not_on_website(a, ctx, locations))

    # Parent companies the About page says Bellhaven acquired from: record the fact on the account.
    for corp in ctx.corporate:
        sentence = corp[ID] != ctx.parent[ID] and ctx.about_mentions(corp[ID])
        if sentence and sentence not in (corp.get(F_NOTE) or ""):
            proposals.append(_operator_note(corp, ctx, sentence))

    return proposals, report


# --------------------------------------------------------------------------- proposal builders


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
    conf = CONFIDENCE[match.tier]

    if wrong_parent:
        old = a.get(F_PARENT) or ""
        acquisition = ctx.about_mentions(old) if old else None
        if old and not acquisition:
            conf = "medium" if conf == "high" else conf
            reasoning.append(f"caution: Bellhaven's About page does not mention acquiring from {ctx.parent_label(old)}")
        elif acquisition:
            reasoning.append(f"About page: \"{acquisition}\"")
        sop = sop_evidence(a)
        ev["sop"] = sop
        note = f"Moved from {ctx.parent_label(old)} to {parent[F_NAME]}: listed on the Bellhaven website ({loc.get('url', '')})."
        expect = {F_PARENT: old, F_CHOW: a.get(F_CHOW) or "", F_DUP: a.get(F_DUP) or ""}
        action = {"op": "reparent", "account_id": a[ID], "new_parent_id": parent[ID], "sop_path": sop["path"], "note": note}
        if sop["path"] == DIRECT:
            action["set"] = fixes
            action["expect"] = {**expect, **{k: a.get(k) or "" for k in fixes}}
            ev["plan"] = [f"Set parent of {label(a)} to {parent[F_NAME]}" + (f" and update {', '.join(fixes)}" if fixes else "") + "."]
            ev["changes"] = changes_rows(label(a), a, {F_PARENT: parent[ID], **fixes})
        else:
            action["new_account"] = website_fields(loc)
            action["expect"] = expect
            ev["plan"] = [
                f"Create a new account '{loc['name']}' under {parent[F_NAME]} from the website data "
                "(or reuse one if it already exists at this address).",
                f"Set chow_current_account on {label(a)} to the new account. Nothing else on the old account changes "
                f"(parent stays {ctx.parent_label(old)}, billing history preserved).",
            ]
            ev["changes"] = changes_rows("NEW account", {}, {**action["new_account"], F_PARENT: parent[ID]}) + [
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
        ["Append a note with the physical address; leave the PO Box billing address."] if po_box_note else [])
    ev["changes"] = changes_rows(label(a), a, fixes) + (
        [{"account": label(a), "field": "note (append)", "before": a.get(F_NOTE), "after": po_box_note}] if po_box_note else [])
    kind = "annotate" if not fixes else "rename" if set(fixes) == {F_NAME} else "update"
    ev["summary"] = "Correct parent; " + (f"{', '.join(fixes)} out of date versus the website." if fixes else "billing address is a PO Box.")
    title = (f"Update {a[F_NAME]}: " + "; ".join(f"{k} -> {v}" for k, v in fixes.items())) if fixes else f"Note physical address on {a[F_NAME]}"
    return {"kind": kind, "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": key,
            "title": title, "confidence": conf, "action": action, "evidence": ev}


def _duplicate(loc, key, dup, survivor, ctx, why=None):
    a = dup.account
    moved = [c["contact_id"] for c in ctx.contacts[a[ID]]]
    note = f"Duplicate of {label(survivor)}: same facility as Bellhaven website listing '{loc['name']}' ({loc['address']})."
    action = {"op": "update", "account_id": a[ID], "set": {F_DUP: survivor[ID], F_STATUS: INACTIVE},
              "expect": {F_DUP: a.get(F_DUP) or "", F_CHOW: a.get(F_CHOW) or ""}, "note": note,
              "move_contacts": moved, "move_contacts_to": survivor[ID]}
    conf = "high" if dup.tier in "AB" else "medium"
    reasons = (why or dup.signals) + ["kept copy chosen by: earlier decisions, match strength, already under Bellhaven, "
                                      "phone/administrator confirmation, Active, billing history, contacts, then lowest id"]
    if money(a.get(F_REV)) > 0:
        conf = "medium"
        reasons.append("this copy has revenue history but no outstanding AR; only the duplicate flag and status change")
    plan = [f"Set duplicate_of_account = {survivor[ID]} and status = Inactive on {label(a)}; append a note."]
    if moved:
        plan.append(f"Move {len(moved)} contact(s) to {label(survivor)} (only those still on this account).")
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


def _chow_duplicate(loc, key, dup, survivor, ctx):
    """An extra copy under the previous owner that carries revenue AND AR: the SOP says preserve it untouched."""
    a = dup.account
    sop = sop_evidence(a)
    return {
        "kind": "chow_duplicate", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": key,
        "title": f"Point {a[F_NAME]} ({ctx.parent_label(a.get(F_PARENT))}) at {survivor[F_NAME]} via CHOW",
        "confidence": "high" if dup.tier in "AB" else "medium",
        "action": {"op": "chow_pointer", "account_id": a[ID], "chow_target_id": survivor[ID],
                   "expect": {F_CHOW: a.get(F_CHOW) or "", F_DUP: a.get(F_DUP) or ""}},
        "evidence": {
            "summary": f"Previous owner's copy of '{loc['name']}' with billing history and open AR.",
            "reasoning": dup.signals + ["marking it Inactive/duplicate would alter an account the billing SOP says to leave as is"],
            "sop": sop,
            "comparison": comparison(loc, a),
            "plan": [f"Set chow_current_account on {label(a)} to {label(survivor)}. Nothing else changes."],
            "changes": [{"account": label(a), "field": F_CHOW, "before": a.get(F_CHOW), "after": survivor[ID]}],
            "related": [related_row("previous owner's account", a, ctx), related_row("live account", survivor, ctx)],
        },
    }


def _create(loc, key, ctx, lookalikes):
    fields = {**website_fields(loc), F_PARENT: ctx.parent[ID]}
    reasons = ["no CRM account matches this street address, and no same-name account in this city is confirmed by phone or administrator"]
    if "directory" not in loc.get("found_on", ["directory"]):
        reasons.append(f"found only on {', '.join(loc['found_on'])} (not in the paginated directory)")
    for m in lookalikes:
        reasons.append(f"lookalike NOT linked: {label(m.account)} under {ctx.parent_label(m.account.get(F_PARENT))}: {'; '.join(m.signals[1:])}")
    note = f"Created from Bellhaven website listing ({loc.get('url', '')})."
    if lookalikes:
        note += " Not the same facility as " + "; ".join(
            f"{label(m.account)} ({m.account.get(F_STREET)}, {m.account.get(F_CITY)} {m.account.get(F_STATE)})" for m in lookalikes) + "."
    same_city = any(N.city(m.account.get(F_CITY)) == N.city(loc["city"]) for m in lookalikes)
    return {
        "kind": "create", "subject": f"location:{key}|{N.name_core(loc['name'])}", "location_key": key,
        "title": f"Create account for {loc['name']} ({loc['city']}, {loc['state']})",
        "confidence": "medium" if same_city else "high",
        "action": {"op": "create", "fields": fields, "note": note},
        "evidence": {
            "summary": "Listed on the Bellhaven website; no CRM account for this facility.",
            "reasoning": reasons,
            "comparison": [{"field": k, "website": v, "crm": "—", "match": False} for k, v in fields.items() if k != F_PARENT],
            "plan": [f"Create '{loc['name']}' under {ctx.parent[F_NAME]}, status Active (reusing an account already at this address if one appears)."],
            "changes": changes_rows("NEW account", {}, fields),
            "related": [related_row("lookalike (not linked)", m.account, ctx) for m in lookalikes],
        },
    }


def _takers(a, ctx):
    """Live accounts of OTHER operators at the same street, ZIP and city: the facility's likely new owner."""
    street, zip_ = N.street(a.get(F_STREET)), N.zip5(a.get(F_ZIP))
    if not street or not zip_:
        return []
    out = []
    for o in ctx.facilities:
        parent = ctx.by_id.get(o.get(F_PARENT) or "")
        if (o[ID] == a[ID] or not parent or not ctx.is_corporate(parent) or parent[ID] == ctx.parent[ID]
                or o.get(F_STATUS) != ACTIVE or o.get(F_DUP) or o.get(F_CHOW)
                or a[ID] in (o.get(F_DUP), o.get(F_CHOW)) or o[ID] in (a.get(F_DUP), a.get(F_CHOW))):
            continue
        if (N.street(o.get(F_STREET)) == street and N.zip5(o.get(F_ZIP)) == zip_
                and N.city(o.get(F_CITY)) == N.city(a.get(F_CITY))):
            out.append(o)
    return out


def _not_on_website(a, ctx, locations):
    takers = _takers(a, ctx)
    if len(takers) == 1:
        return _moved_away(a, takers[0], ctx)

    near = sorted(locations, key=lambda l: -N.similarity(N.name_core(l["name"]), N.name_core(a.get(F_NAME))))[:1]
    reasons = [f"parented to Bellhaven, but no website location matches its address ({a.get(F_STREET)}, {a.get(F_CITY)}) or name"]
    if takers:
        reasons.append("several other operators have accounts at this address: " + ", ".join(label(t) for t in takers) + "; not guessing")
    else:
        reasons.append("no other operator's account exists at this address, so the new owner (if any) is unknown")
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
            "related": [related_row("account", a, ctx)] + [related_row("other operator at address", t, ctx) for t in takers],
        },
    }


def _moved_away(a, taker, ctx):
    owner = operator_name(ctx.by_id[taker[F_PARENT]])
    sop = sop_evidence(a)
    common = {"kind": "moved_away", "subject": f"account:{a[ID]}", "account_id": a[ID], "location_key": None, "confidence": "high"}
    reasoning = [f"same street address, ZIP and city: {a.get(F_STREET)}, {a.get(F_CITY)} {a.get(F_ZIP)}",
                 f"{label(taker)} is an Active account under {ctx.parent_label(taker[F_PARENT])}"]
    related = [related_row("Bellhaven account (old owner)", a, ctx), related_row("current owner's account", taker, ctx)]
    if sop["path"] == CHOW:
        return {**common,
                "title": f"{a[F_NAME]} now belongs to {owner} (CHOW pointer)",
                "action": {"op": "chow_pointer", "account_id": a[ID], "chow_target_id": taker[ID],
                           "expect": {F_PARENT: a.get(F_PARENT), F_CHOW: a.get(F_CHOW) or "", F_DUP: a.get(F_DUP) or ""}},
                "evidence": {
                    "summary": f"Not on the Bellhaven website; {owner} has an account at the same address.",
                    "reasoning": reasoning, "sop": sop, "related": related,
                    "plan": [f"Set chow_current_account on {label(a)} to {label(taker)}, the existing account under {owner}. "
                             "Nothing else on the old account changes.",
                             "No new account is created: the new owner's account already exists, and another would be a duplicate."],
                    "changes": [{"account": label(a), "field": F_CHOW, "before": a.get(F_CHOW), "after": taker[ID]}]}}
    # No AR to protect. Re-parenting would leave two live accounts for one facility under the new owner,
    # so the Bellhaven copy becomes the duplicate of the account the new owner already has.
    as_location = {"name": taker[F_NAME], "address": taker.get(F_STREET) or "", "city": taker.get(F_CITY) or "",
                   "state": taker.get(F_STATE) or "", "zip": taker.get(F_ZIP) or "", "phone": taker.get(F_PHONE) or "",
                   "care_offerings": [taker[F_CARE]] if taker.get(F_CARE) else []}
    dup = _duplicate(as_location, None, Match(a, "A", reasoning), taker, ctx)
    dup["evidence"]["sop"] = sop
    dup["evidence"]["summary"] = (f"Not on the Bellhaven website; {owner} already has an account at the same address. "
                                  "No outstanding AR, so the Bellhaven copy is retired as its duplicate.")
    return {**dup, **common, "title": f"{a[F_NAME]} now belongs to {owner}: mark duplicate of {label(taker)}"}


def _operator_note(corp, ctx, sentence):
    note = (f"Bellhaven Senior Living website (About page): \"{sentence}\" "
            "Facilities that joined Bellhaven are tracked under the Bellhaven parent account.")
    remaining = [c for c in ctx.children[corp[ID]] if c.get(F_STATUS) == ACTIVE and not c.get(F_DUP)]
    return {
        "kind": "operator_note", "subject": f"account:{corp[ID]}", "account_id": corp[ID], "location_key": None,
        "title": f"Note on {corp[F_NAME]}: About page says communities joined Bellhaven",
        "confidence": "high",
        "action": {"op": "update", "account_id": corp[ID], "set": {}, "expect": {}, "note": note},
        "evidence": {
            "summary": "Parent company named on Bellhaven's About page as a source of acquired communities.",
            "reasoning": [f"About page: \"{sentence}\"",
                          "status is NOT changed: the sentence proves communities moved, not that the company ceased to exist",
                          f"{len(remaining)} Active non-duplicate account(s) still under it in the current CRM"],
            "plan": [f"Append the quoted sentence as a note on {label(corp)}. No other change."],
            "changes": [{"account": label(corp), "field": "note (append)", "before": corp.get(F_NOTE), "after": note}],
            "related": [related_row("child", c, ctx) for c in ctx.children[corp[ID]]],
        },
    }
