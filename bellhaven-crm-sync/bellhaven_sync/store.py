"""SQLite state: pipeline runs, proposals and reviewer decisions.

Idempotency rule: every proposal has a fingerprint built from *what* it would
change (kind + subject + the exact target values). A re-run that produces the
same fingerprint never creates a second row, and a fingerprint that already
has a decision (approved / rejected / applied) is never shown again. If the
underlying facts change (e.g. the website lists a new name), the fingerprint
changes and the new proposal is reviewed on its own merits.
"""
import hashlib
import json
import sqlite3
from pathlib import Path
from datetime import datetime, timezone

from . import config

PENDING = "pending"
APPROVED = "approved"  # approved, write in progress or failed
APPLIED = "applied"  # approved and written to the CRM
REJECTED = "rejected"
STALE = "stale"  # pending, but the latest run no longer produces it
DECIDED = (APPROVED, APPLIED, REJECTED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    summary_json TEXT
);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    account_id TEXT,
    location_key TEXT,
    title TEXT NOT NULL,
    confidence TEXT NOT NULL,
    action_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_run INTEGER,
    last_seen_run INTEGER,
    decided_at TEXT,
    decision_note TEXT,
    applied_at TEXT,
    result_json TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path=None):
    path = path or config.DB_PATH
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def fingerprint(kind, subject, action):
    """Stable hash of what a proposal would do.

    Evidence and the 'expect' precondition (current CRM values) are excluded:
    the decision is about the target state, not about today's snapshot.
    """
    target = {k: v for k, v in action.items() if k != "expect"}
    payload = json.dumps([kind, subject, target], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


def start_run(conn):
    cur = conn.execute("INSERT INTO runs (started_at, status) VALUES (?, 'running')", (now(),))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id, status, summary):
    conn.execute(
        "UPDATE runs SET finished_at=?, status=?, summary_json=? WHERE id=?",
        (now(), status, json.dumps(summary), run_id),
    )
    conn.commit()


def upsert_proposals(conn, run_id, proposals):
    """Record this run's proposals. Returns counts for the run summary."""
    counts = {"new": 0, "still_pending": 0, "already_decided": 0, "marked_stale": 0}
    seen = set()
    for p in proposals:
        fp = fingerprint(p["kind"], p["subject"], p["action"])
        seen.add(fp)
        row = conn.execute("SELECT id, status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO proposals (fingerprint, kind, account_id, location_key, title,
                   confidence, action_json, evidence_json, status, created_run, last_seen_run)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    fp, p["kind"], p.get("account_id"), p.get("location_key"), p["title"],
                    p["confidence"], json.dumps(p["action"]), json.dumps(p["evidence"]),
                    PENDING, run_id, run_id,
                ),
            )
            counts["new"] += 1
        elif row["status"] in DECIDED:
            conn.execute("UPDATE proposals SET last_seen_run=? WHERE id=?", (run_id, row["id"]))
            counts["already_decided"] += 1
        else:  # pending or stale: refresh evidence so the reviewer sees today's facts
            conn.execute(
                "UPDATE proposals SET status=?, evidence_json=?, title=?, confidence=?, last_seen_run=? WHERE id=?",
                (PENDING, json.dumps(p["evidence"]), p["title"], p["confidence"], run_id, row["id"]),
            )
            counts["still_pending"] += 1

    # Pending items this run did not reproduce are no longer supported by the data.
    for row in conn.execute("SELECT id, fingerprint FROM proposals WHERE status=?", (PENDING,)).fetchall():
        if row["fingerprint"] not in seen:
            conn.execute("UPDATE proposals SET status=? WHERE id=?", (STALE, row["id"]))
            counts["marked_stale"] += 1
    conn.commit()
    return counts


def list_proposals(conn, status=None):
    if status:
        rows = conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return [_hydrate(r) for r in rows]


def get_proposal(conn, proposal_id):
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    return _hydrate(row) if row else None


def set_status(conn, proposal_id, status, note=None, result=None):
    fields = {"status": status}
    if status in DECIDED:
        fields["decided_at"] = now()
    if note is not None:
        fields["decision_note"] = note
    if result is not None:
        fields["result_json"] = json.dumps(result)
    if status == APPLIED:
        fields["applied_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE proposals SET {sets} WHERE id=?", (*fields.values(), proposal_id))
    conn.commit()


def latest_run(conn):
    row = conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    run = dict(row)
    run["summary"] = json.loads(run["summary_json"] or "{}")
    return run


def status_counts(conn):
    return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}


def _hydrate(row):
    p = dict(row)
    p["action"] = json.loads(p.pop("action_json"))
    p["evidence"] = json.loads(p.pop("evidence_json"))
    p["result"] = json.loads(p.pop("result_json") or "null")
    return p
