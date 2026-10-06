"""Daily job: scrape website -> read CRM -> build proposals -> store for review.

Read-only against the CRM. Writes happen only in the review app after approval.
Run:  BELLHAVEN_API_TOKEN=... python -m bellhaven_sync.pipeline
"""
import json
import sys

from . import config, matcher, scraper, store
from .crm import CRMClient

# If the scrape suddenly returns far fewer locations than last time, the site is
# probably down or its HTML changed. Stop rather than flag every account as gone.
MIN_FRACTION_OF_LAST_RUN = 0.5


class ScrapeLooksBroken(RuntimeError):
    pass


def _previous_location_count(conn):
    for row in conn.execute("SELECT summary_json FROM runs WHERE status='ok' ORDER BY id DESC"):
        n = json.loads(row["summary_json"] or "{}").get("website_locations")
        if n:
            return n
    return None


def _touches(p):
    a = p["action"]
    return set(filter(None, [a.get("account_id"), a.get("chow_target_id"), a.get("move_contacts_to"),
                             (a.get("set") or {}).get("duplicate_of_account")]))


def run(client=None, conn=None, site=None):
    """site: optional pre-scraped {"locations", "about_text", "claimed_count"} (tests / replays)."""
    conn = conn or store.connect()
    client = client or CRMClient()
    run_id = store.start_run(conn)
    try:
        site = scraper.scrape_site() if site is None else site
        locations = site["locations"]
        prev = _previous_location_count(conn)
        if not locations or (prev and len(locations) < prev * MIN_FRACTION_OF_LAST_RUN):
            raise ScrapeLooksBroken(f"Scraped {len(locations)} locations (last good run: {prev}). Aborting.")

        # Fewer communities than the website itself claims => we may have missed some; then do not
        # infer "no longer on the website" for anything.
        claims = [c for c in (site.get("claimed_count"), site.get("directory_claim")) if c]
        site_complete = all(len(locations) >= c for c in claims)

        accounts = client.list_accounts()
        contacts = client.list_contacts()
        proposals, report = matcher.build_proposals(
            locations, accounts, contacts, site.get("about_text", ""), site_complete=site_complete)

        # Never propose around a write that was approved but has not finished (e.g. a CHOW whose
        # pointer step failed): the half-written state would look like a duplicate.
        busy = store.in_flight_account_ids(conn)
        held_back = [p for p in proposals if _touches(p) & busy]
        proposals = [p for p in proposals if not (_touches(p) & busy)]
        counts = store.upsert_proposals(conn, run_id, proposals)

        snapshot_dir = config.DB_PATH.parent / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / f"run_{run_id}.json").write_text(json.dumps(
            {"site": site, "accounts": accounts, "contacts": contacts, "report": report}, indent=2, default=str))

        claimed = site.get("claimed_count")
        summary = {
            "website_locations": len(locations),
            "website_claims": claimed,
            "crm_accounts": len(accounts),
            "confirmed_matches": len(report["confirmed"]),
            "proposals_this_run": len(proposals),
            "new_for_review": counts["new"],
            "still_pending": counts["still_pending"],
            "skipped_already_decided": counts["already_decided"],
            "marked_stale": counts["marked_stale"],
        }
        if not site_complete:
            summary["warning"] = (f"website claims {claims} communities but {len(locations)} were found; "
                                  "'not on website' checks skipped this run")
        if held_back:
            summary["held_back_for_in_flight_writes"] = len(held_back)
        store.finish_run(conn, run_id, "ok", summary)
        return summary
    except Exception as exc:
        store.finish_run(conn, run_id, "failed", {"error": str(exc)})
        raise


if __name__ == "__main__":
    try:
        print(json.dumps(run(), indent=2))
    except Exception as exc:  # non-zero exit so cron / Actions alert on failure
        print(f"pipeline failed: {exc}", file=sys.stderr)
        sys.exit(1)
