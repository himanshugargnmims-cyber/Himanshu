# Bellhaven CRM ownership sync

Keeps the CRM's facility → parent links for Bellhaven Senior Living in line with
Bellhaven's public website. A daily run reads the website and the CRM and
**proposes** changes. Only a human approving a proposal in the review app writes
to the CRM.

```
website ──scraper──┐
                   ├─ matcher ─ proposals ─ SQLite (state/) ─ review app ─ approve ─ CRM API
CRM API (read) ────┘                                           └─ reject ─ remembered, never re-proposed
```

## Run it

```bash
cd bellhaven-crm-sync
pip install -r requirements.txt
export BELLHAVEN_API_TOKEN=bh_...            # your token; never commit it

python -m bellhaven_sync.pipeline           # scrape + match + queue proposals (read-only)
python -m bellhaven_sync.app                # review UI on http://127.0.0.1:5000
python -m pytest -q                         # tests (fake CRM, no network)
```

## Reviewing

- The queue lists the highest-stakes items first: ownership moves and the billing SOP, then duplicates, creates, renames, notes.
- Each proposal page shows the evidence (website vs CRM, match signals, lookalikes not linked), the billing SOP check and every field it will write.
- **Approve** writes immediately through the API. It re-reads the CRM first and refuses if anything changed since the proposal (state `stale`; the next run re-proposes with fresh data).
- A write that errors is `failed`. **Retry** continues from saved progress (e.g. a CHOW whose new account already exists), and **Abandon** gives up on it.
- **Reject** is remembered: the same change is never proposed again.

## Layout

| File | Role |
|---|---|
| `bellhaven_sync/scraper.py` | Pull every community from the website |
| `bellhaven_sync/crm.py` | CRM API client |
| `bellhaven_sync/normalize.py` | Address / name normalisation |
| `bellhaven_sync/matcher.py` | Link locations to accounts, classify, build proposals with evidence |
| `bellhaven_sync/store.py` | Runs, proposals, decisions; fingerprint idempotency; atomic claim (pending → applying → applied/failed/stale) |
| `bellhaven_sync/apply.py` | The only code that writes; billing SOP (CHOW) enforced here |
| `bellhaven_sync/app.py` | Flask review app |
| `bellhaven_sync/pipeline.py` | Daily entry point |
| `deploy/crontab.txt` | Primary schedule |
| `deploy/github-actions-daily.yml` | Alternative schedule |
| `tests/` | 45 tests: fake CRM with the real API's validation; real-snapshot regression; one test per review finding |
| `WRITEUP.md` | Decisions, judgment calls, verification, live end state |
