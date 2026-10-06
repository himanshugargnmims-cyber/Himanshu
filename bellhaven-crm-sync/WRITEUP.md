# Bellhaven CRM ownership sync: writeup

## Time spent

> **Fill in honestly before submitting:** ___ h ___ min of focused time.
> (Includes directing the AI tools and checking their work, which the brief says is the point.)

## What I built

| Requirement | Where | Notes |
|---|---|---|
| 1. Scraper | `bellhaven_sync/scraper.py` | Directory pages + homepage + About. Returns name, address, city, state, zip, care offerings, phone, administrator. |
| 2. Matching + classification | `bellhaven_sync/matcher.py` | Address-first tiers, corroboration from phone and administrator, lookalike rejection, survivor choice for duplicates. |
| 3. Review app | `bellhaven_sync/app.py` | Flask, local. Evidence, SOP check, before/after per write. Approve writes through the API; nothing else does. |
| 4. Daily + re-run safe | `pipeline.py`, `store.py`, `deploy/` | Fingerprint idempotency in SQLite; cron entry (primary) and a GitHub Actions file. |
| SOP | `bellhaven_sync/apply.py` | `sop_path()`; re-checked on live data at the moment of approval. |

```
website ──scraper──┐
                   ├─ matcher ─ proposals ─ SQLite ─ review app ─ Approve ─ apply.py ─ CRM API (write)
CRM API (read) ────┘                                            └─ Reject ─ remembered, never re-proposed
```

## What the data contained, and what I decided

The website lists 35 communities; the CRM has 121 accounts (6 parent companies). The About page says Bellhaven
"welcomed the Harborview Care Group family of communities" in 2025 and "select communities ... from Cedar Trail" in 2026.

### Matching rules (strongest first)

1. **A**: street address matches (after normalising "Road/Rd", "Northwest/NW", "Pk/Pike", suite numbers) and ZIP or city matches. City is enough when the ZIP is a typo.
2. **B**: same street number, ZIP, direction and street type; only the street name's spelling may differ. So "100 Oak St" never matches "100 Oak Ave", and "E Main" never matches "W Main".
3. **C**: same city and a different address, confirmed by (same name + phone or administrator) or by phone + administrator together. The second form lets a renamed PO Box community still link.
4. **D**: same name and city, account already under Bellhaven, but the address differs and nothing confirms it. Linked at *low* confidence so the reviewer checks it, rather than creating a duplicate.
5. **Lookalike**: a similar name without A–D. Never linked; shown to the reviewer as evidence.
6. Communities that share one street address on the website are matched by name and never auto-merged.

Phones in the CRM are unreliable (many don't match the website). So a matching phone counts as evidence, but a mismatch is never held against a match.

### Cases

| Case | Accounts | Decision | Why |
|---|---|---|---|
| Homepage-only community | Bellhaven Meadows of Findlay (no parent) | Re-parent to Bellhaven directly | Not in the paginated directory; found via homepage ("35 communities" vs "34 listed"). Revenue 22k but AR 0, so it's a direct re-parent. |
| CHOW | Bellhaven of Marietta, Bellhaven of Tiffin (Cedar Trail) | New account under Bellhaven; old account gets only `chow_current_account` | Revenue history AND AR > 0. |
| Direct re-parent | Crossings of Lima (Harborview), Cedar Trail of Zanesville | Re-parent (+ rename Zanesville) | No AR, or no revenue. |
| Sold away | Bellhaven of Sandusky (rev 130k, AR 5.2k) | `chow_current_account` → existing *Millstone Care of Sandusky* | Not on the website; Millstone has an account at the identical address. It has AR, so the old account must not move. The new owner's account already exists, so I point at it instead of creating a duplicate. |
| Triple duplicates | Kettering (no parent / Cedar Trail / Harborview), Monroe (Bellhaven / Cedar Trail / Harborview) | Keep one, others `duplicate_of_account` + Inactive; rename/re-parent the survivor | Same street address. Survivor rule: an earlier duplicate decision, then already under Bellhaven, then match strength, then phone/admin confirmation, then Active, then billing history, then contacts, then lowest id. A duplicate can only be written once its survivor is live under Bellhaven (Kettering: the move goes first). If a losing copy under the old owner carries revenue AND AR, it is not deactivated: it gets a CHOW pointer to the live account (SOP: leave it as is). None in today's data. |
| Duplicates across old owners | Port Clinton, Shores of Erie (Harborview copies) | Harborview copies → duplicates of the Bellhaven ones | The Bellhaven copies carry the website administrator as a contact. |
| Duplicate within Bellhaven | Bellhaven of Owosso ×2 | Keep the copy whose phone and administrator match the website; move the other copy's contact to it | Reps keep the admissions director's contact. |
| Rebrands / outdated names | Riverbend Manor → Bellhaven of Chagrin Falls; Sunny Acres → Bellhaven Willow Creek; Chesterton Senior Commons → Bellhaven of Chesterton; 4 minor spelling fixes | Rename | Same address; Chagrin Falls and Chesterton are also confirmed by the administrator contact. |
| ZIP typo | Bellhaven of Portsmouth (45626 vs 45662) | Fix ZIP | Street and city match; phone and administrator confirm. |
| PO Box | Bellhaven of Ashtabula (`billing_street` = PO Box 517) | Match; **keep** the PO Box, add the physical address to the note | The field is a *billing* address and a PO Box is a valid one; overwriting could misroute invoices. Name, city, ZIP, phone and administrator all confirm the match. |
| Lookalike, other operator | *Union Square Senior Living* (Juniper Point, 240 Market St) vs website *Bellhaven at Union Square* (118 Union Square Dr) | **Create** a new account; leave Juniper Point's alone | Different street, administrator (Dale Croft vs Phil Holloway) and phone; Juniper Point isn't among Bellhaven's acquisitions. |
| Lookalike, other state | *Amberly Manor* (Colorado Springs, CO) vs website Amberly Manor (Hudson, OH) | **Create** | Different state; Bellhaven operates in OH/MI/IN/PA only. |
| Lookalike, same city | *Maplewood Senior Care Center* (Stonebridge, 431 Main St) | Not linked, not a duplicate | Different street address from Bellhaven of Maplewood. |
| New facilities | Bellhaven of Batavia, Bellhaven of Carlisle | Create | No account at the address; Carlisle PA is not New Carlisle OH. |
| Under Bellhaven, not on website | Bellhaven Care Center of Alliance, Bellhaven of Coldwater | Status Needs Review + note; parent unchanged | Not listed and no other operator's account at the address, so the new owner (or closure) is unknown. Moving the parent would be a guess; Inactive would claim a closure I can't see. |
| Acquired operators | Harborview Care Group, Cedar Trail Communities (parent accounts) | Note quoting the About page; **status unchanged** | The About sentence proves the communities moved, not that the companies ceased to exist, and the brief never asks to deactivate parent companies. My first version set Harborview Inactive; independent review flagged that as an unprovable claim, and I changed it. |
| Already right | 16 locations | No write | Includes Bellhaven Terrace of Akron, which has AR but is already under Bellhaven, so there's no move and no SOP. |

## SOP handling

- `apply.sop_path()` is the only place the rule lives: `lifetime_revenue > 0 and outstanding_ar > 0` → CHOW, else direct.
- The proposal shows which path applies. **At approval the account is re-read and the SOP re-evaluated**. If billing changed in between, the write is refused and the proposal goes stale for a fresh run.
- CHOW writes exactly one field on the old account (`chow_current_account`). Not even a note, because the SOP says "leave the existing account exactly as it is". The new account carries the explanatory note.
- A CHOW that fails halfway (account created, pointer not yet set) saves the new account id first. A retry finishes the job instead of creating a second account.

## Re-run safety

- Each proposal's fingerprint = hash(kind, subject, target values), **excluding** today's CRM values, notes and evidence. Re-runs never duplicate a pending item. Approved/rejected items are skipped forever, even after the survivor they mention is renamed.
- Approving is an atomic claim (`pending → applying`), so a double click cannot write twice. A failed write keeps its progress (e.g. a created account id), and Retry continues from there. Creates first look for an identical live copy (same parent, name, street and ZIP), which covers a response lost after the CRM committed, without ever adopting a different community at the same address.
- While a write is unfinished, the daily run holds back any proposal touching the same accounts. Otherwise half-written state (a new CHOW account without its pointer) would look like a duplicate.
- If the scrape finds fewer communities than the website claims (homepage "35", directory "34 listed"), "not on website" checks are skipped for that run.
- After approval the CRM agrees with the website, so the matcher generates nothing for those items in the first place. A test replays the real snapshot: approve all 31, re-run, 0 proposals. The live run did the same (below).
- Pending items that a later run no longer produces become *stale* and drop out of the queue, so a reviewer never approves something the data no longer supports.
- If a scrape returns fewer than half of the last run's locations, the run aborts. Otherwise an outage would flag every account "not on website".
- Every write re-reads the account and checks that the fields it expects are unchanged (`expect`), so a concurrent human edit is never overwritten.

## Judgment calls a reviewer might disagree with

1. **PO Box (Ashtabula)**: I kept the billing address. The alternative is to overwrite it with the street address and keep the PO Box in the note.
2. **Not on website → Needs Review, not Inactive**: I chose not to assert a closure or sale I can't see.
3. **Sandusky**: I pointed the CHOW at the existing Millstone account instead of creating a new one. Creating one would satisfy the SOP's wording but leave two Millstone accounts for one facility.
4. **Harborview parent**: I left its status alone and added a note quoting the About page. Setting it Inactive is the alternative if you know the company was dissolved.
5. **Kettering survivor**: no copy had billing, contacts, a confirming phone/administrator or a Bellhaven parent. The choice is deterministic (lowest id: the parentless copy) but arbitrary. Every check that matters still holds: exactly one live Kettering account under Bellhaven, and the other two point at it.
6. **Phones not updated**: CRM phones disagree with the website for most facilities, and they may be billing or main-office lines. I didn't propose mass phone changes the brief didn't ask for.

## How I used AI tools, and how I checked them

- Claude Code wrote most of the code under my direction.
- The matching rules came from reading the raw data first: printing all 121 accounts by parent, diffing website administrators against CRM contacts, and comparing phones.
- **Independent verification:** three agents re-derived the correct outcome for every location from the raw data without seeing my proposals. Judges then compared and argued against my proposals; two reviewers audited the SOP/idempotency code and the matcher. *(Results: see "Verification" below.)*
- Tests pin every case above on a saved snapshot, so a live change in the demo that breaks one fails loudly.

## Verification

1. **Blind re-derivation.** Three agents each took a third of the website, read only the raw website/CRM data and the rules, and wrote down the correct end state. A judge per slice compared that with my proposals and argued against mine.
   - 42 items agreed.
   - **No dispute changed a scored field.**
   - Remaining disputes were judgment calls (phones, extra notes, the Kettering survivor, PO Box handling) or artefacts of the summary I gave the judges.
   - The Harborview deactivation was flagged as an unprovable claim, and I changed it to a note.
2. **Code review.** Two reviewers attacked the write path and the matcher and found 1 critical and about 15 major or minor defects, almost all of which reproduced. The headline one: a double-clicked Approve could create two accounts. Every finding I fixed has a regression test (`tests/test_pipeline.py`, section "regressions from the code review").
3. **Re-review of the fixes.** Three more reviewers attacked the hardened code: write path, matcher under next-day data changes, and live-API readiness. All three said today's queue was safe. The live-readiness agent replayed the 31 approvals in 40 random orders against a fake CRM with the real API's validation rules: zero failures and an identical end state every time. The rest were failure modes on *later* runs; I fixed the ones that protect data:
   - The new-owner search no longer treats an operator Bellhaven bought from as a facility's new owner. Before the fix, Port Clinton dropping off the site would have retired the live account into Harborview's stale copy.
   - Pointer targets are re-checked at write time.
   - Duplicates wait for their survivor's move.
   - Approve is bound to the version the reviewer saw.
   - Failed writes can be abandoned.

   45 tests in total.

## Live run and end state (2026-10-06)

- Pre-flight: the CRM was byte-identical to the verified snapshot (121 accounts, 67 contacts). The pipeline produced the same 31 proposals.
- All 31 were approved through the review app's Approve endpoint, in the app's order (ownership moves before duplicates). **31 applied, 0 failed, 0 refused.**
- Verified against the live API afterwards:
  - **35 Active, unflagged accounts under Bellhaven, one per website community, names identical to the website.**
  - Marietta, Tiffin and Sandusky old accounts: the only changed field is `chow_current_account`. Marietta and Tiffin point at new Bellhaven accounts with the full website data; Sandusky points at Millstone Care of Sandusky.
  - 7 duplicates are Inactive and point at live Bellhaven accounts. The Owosso duplicate's contact moved to the kept copy.
  - The 3 lookalike accounts are untouched, and no account outside the 27 intended ones changed.
  - 6 new accounts: 4 new communities + 2 CHOW replacements. Alliance and Coldwater are Needs Review.
- **Re-run: 35 confirmed matches, 0 proposals.**

## Limitations / what I'd do next in production

- A rejection is remembered per exact change. If the website later reformats a value ("St" → "Street"), the same idea can come back once for review. That's acceptable noise, but per-field decision memory would remove it.
- Co-located communities (two listings at one street address) are matched by name and never auto-merged. That path is tested but not exercised by today's data.
- Stale administrator contacts are visible in each proposal's contacts column but not changed. Sycamore Ridge, Lima, Saline and Wooster list a different administrator on the website than in the CRM. The API can re-point contacts but not create them, so this needs a rep.
- SQLite + local review app means the state lives on one machine. In production I'd use Postgres and a hosted app with SSO, and attribute decisions to reviewers.
- The website is the only ownership source. In production I'd add CMS ownership/CHOW data for skilled nursing and state licensing data for assisted living.
- Contacts on CHOW'd accounts stay on the old account (the SOP says leave it as is). A follow-up task should ask reps which contacts moved with the facility.
