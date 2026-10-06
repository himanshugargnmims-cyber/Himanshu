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

## Layout

| File | Role |
|---|---|
| `bellhaven_sync/scraper.py` | Pull every community from the website |
| `bellhaven_sync/crm.py` | CRM API client |
| `bellhaven_sync/normalize.py` | Address / name normalisation |
| `bellhaven_sync/matcher.py` | Link locations to accounts, classify, build proposals with evidence |
| `bellhaven_sync/store.py` | Runs, proposals, decisions; fingerprint-based idempotency |
| `bellhaven_sync/apply.py` | The only code that writes; billing SOP (CHOW) enforced here |
| `bellhaven_sync/app.py` | Flask review app |
| `bellhaven_sync/pipeline.py` | Daily entry point |
| `deploy/crontab.txt` | Primary schedule |
| `deploy/github-actions-daily.yml` | Alternative schedule |
