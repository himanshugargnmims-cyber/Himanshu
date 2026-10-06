"""Local review app. Nothing reaches the CRM unless a reviewer clicks Approve here.

Run:  BELLHAVEN_API_TOKEN=... python -m bellhaven_sync.app   ->  http://127.0.0.1:5000
"""
import traceback
from urllib.parse import urlparse

from flask import Flask, abort, redirect, render_template, request, url_for

from . import apply as applier
from . import pipeline, store
from .crm import CRMClient

app = Flask(__name__)

# Highest-stakes first: ownership moves and billing SOP, then dedupe, new accounts, field fixes.
KIND_ORDER = ["reparent", "moved_away", "chow_duplicate", "duplicate", "create", "rename", "update", "annotate",
              "not_on_website", "operator_note"]


def db():
    return store.connect()


@app.before_request
def same_origin_posts_only():
    """A page open in another tab must not be able to submit Approve for the reviewer."""
    if request.method == "POST":
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin and urlparse(origin).netloc != request.host:
            abort(403)


@app.route("/")
def index():
    status = request.args.get("status", store.PENDING)
    conn = db()
    proposals = store.list_proposals(conn, None if status == "all" else status)
    proposals.sort(key=lambda p: (KIND_ORDER.index(p["kind"]) if p["kind"] in KIND_ORDER else 99, p["id"]))
    return render_template(
        "index.html",
        proposals=proposals,
        status=status,
        counts=store.status_counts(conn),
        run=store.latest_run(conn),
    )


@app.route("/proposal/<int:pid>")
def detail(pid):
    p = store.get_proposal(db(), pid)
    return render_template("detail.html", p=p) if p else ("Not found", 404)


@app.post("/proposal/<int:pid>/approve")
def approve(pid):
    conn = db()
    note = request.form.get("note", "").strip() or None
    if not store.claim(conn, pid, note):  # already applying/applied/rejected (e.g. a double click)
        return redirect(url_for("detail", pid=pid))
    proposal = store.get_proposal(conn, pid)
    try:
        result = applier.apply_proposal(conn, CRMClient(), proposal)
        store.set_status(conn, pid, store.APPLIED, result=result)
    except applier.PreconditionFailed as exc:
        saved = store.get_proposal(conn, pid)["result"] or {}
        store.set_status(conn, pid, store.STALE, result={**saved, "error": str(exc)})
    except Exception as exc:  # FAILED keeps saved progress so Retry continues where it stopped
        saved = store.get_proposal(conn, pid)["result"] or {}
        store.set_status(conn, pid, store.FAILED, result={**saved, "error": str(exc), "trace": traceback.format_exc()})
    return redirect(url_for("detail", pid=pid))


@app.post("/proposal/<int:pid>/reject")
def reject(pid):
    store.reject(db(), pid, request.form.get("note", "").strip() or None)
    return redirect(request.form.get("next") or url_for("index"))


@app.post("/run")
def run_pipeline():
    pipeline.run()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
