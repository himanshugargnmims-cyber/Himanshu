"""SQLite state: pipeline runs, proposals and reviewer decisions.

Idempotency rule: every proposal has a fingerprint built from *what* it would
change (kind + subject + the target values). A re-run that produces the same
fingerprint never creates a second row, and a fingerprint that already has a
decision is never shown again. If the underlying facts change (e.g. the website
lists a new name), the fingerprint changes and the new proposal is reviewed on
its own merits.

Lifecycle:  pending --approve--> applying --> applied
                                          \\-> failed  (retryable; partial results kept)
                                          \\-> stale   (CRM changed under us; re-run proposes afresh)
            pending --reject---> rejected
            pending --(no longer produced by a run)--> stale
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

PENDING = "pending"
APPLYING = "applying"  # claimed by one approval; write in progress
APPLIED = "applied"
FAILED = "failed"  # approved but the write errored; Retry continues from saved progress
REJECTED = "rejected"
STALE = "stale"
DECIDED = (APPLYING, APPLIED, FAILED, REJECTED)
STUCK_AFTER = timedelta(minutes=10)  # an 'applying' row older than this can be re-claimed

# Free text and lists that can change for cosmetic reasons are not part of the decision.
NOT_FINGERPRINTED = ("expect", "note", "move_contacts")

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
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def fingerprint(kind, subject, action):
    """Stable hash of what a proposal would do (target values only)."""
    target = {k: v for k, v in action.items() if k not in NOT_FINGERPRINTED}
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
        else:  # pending or stale: refresh to today's facts (incl. the 'expect' precondition)
            conn.execute(
                """UPDATE proposals SET status=?, action_json=?, evidence_json=?, title=?, confidence=?,
                   last_seen_run=? WHERE id=? AND status IN (?, ?)""",  # never reopen a row claimed meanwhile
                (PENDING, json.dumps(p["action"]), json.dumps(p["evidence"]), p["title"], p["confidence"],
                 run_id, row["id"], PENDING, STALE),
            )
            counts["still_pending"] += 1

    # Pending items this run did not reproduce are no longer supported by the data.
    for row in conn.execute("SELECT id, fingerprint FROM proposals WHERE status=?", (PENDING,)).fetchall():
        if row["fingerprint"] not in seen:
            conn.execute("UPDATE proposals SET status=? WHERE id=? AND status=?", (STALE, row["id"], PENDING))
            counts["marked_stale"] += 1
    conn.commit()
    return counts


def action_version(action):
    """Hash of the exact action a reviewer is looking at; Approve must present it."""
    return hashlib.sha256(json.dumps(action, sort_keys=True, default=str).encode()).hexdigest()[:16]


def is_stuck(p):
    """An 'applying' row whose process died (crash, Ctrl-C, reloader) can be retried after a while."""
    return p["status"] == APPLYING and p["decided_at"] and \
        p["decided_at"] < (datetime.now(timezone.utc) - STUCK_AFTER).isoformat(timespec="seconds")


def claim(conn, proposal_id, note=None, version=None):
    """Atomically move a proposal into 'applying'. False if someone else has it, or if the action
    changed since the reviewer looked at it (version mismatch)."""
    if version is not None:
        p = get_proposal(conn, proposal_id)
        if p is None or action_version(p["action"]) != version:
            return False
    stuck_before = (datetime.now(timezone.utc) - STUCK_AFTER).isoformat(timespec="seconds")
    cur = conn.execute(
        """UPDATE proposals SET status=?, decided_at=?, decision_note=COALESCE(?, decision_note)
           WHERE id=? AND (status IN (?, ?) OR (status=? AND decided_at < ?))""",
        (APPLYING, now(), note, proposal_id, PENDING, FAILED, APPLYING, stuck_before),
    )
    conn.commit()
    return cur.rowcount == 1


def save_result(conn, proposal_id, result):
    """Persist partial progress (e.g. a created account id) without changing status."""
    conn.execute("UPDATE proposals SET result_json=? WHERE id=?", (json.dumps(result), proposal_id))
    conn.commit()


def set_status(conn, proposal_id, status, note=None, result=None):
    fields = {"status": status}
    if status in DECIDED and status != APPLIED:
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


def reject(conn, proposal_id, note=None):
    """Reject a pending proposal, or abandon a failed/stale one (it then stops holding back its accounts)."""
    cur = conn.execute(
        "UPDATE proposals SET status=?, decided_at=?, decision_note=? WHERE id=? AND status IN (?, ?, ?)",
        (REJECTED, now(), note, proposal_id, PENDING, FAILED, STALE),
    )
    conn.commit()
    return cur.rowcount == 1


def in_flight_account_ids(conn):
    """Accounts touched by approved-but-unfinished writes; the matcher must not propose around them."""
    ids = set()
    stale_with_writes = [p for p in list_proposals(conn, STALE) if (p["result"] or {}).get("created_account_id")]
    for p in list_proposals(conn, APPLYING) + list_proposals(conn, FAILED) + stale_with_writes:
        a, r = p["action"], p["result"] or {}
        ids.update(filter(None, [a.get("account_id"), a.get("chow_target_id"), a.get("move_contacts_to"),
                                 (a.get("set") or {}).get("duplicate_of_account"), r.get("created_account_id")]))
    return ids


def list_proposals(conn, status=None):
    if status:
        rows = conn.execute("SELECT * FROM proposals WHERE status=? ORDER BY id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM proposals ORDER BY id").fetchall()
    return [_hydrate(r) for r in rows]


def get_proposal(conn, proposal_id):
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    return _hydrate(row) if row else None


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
