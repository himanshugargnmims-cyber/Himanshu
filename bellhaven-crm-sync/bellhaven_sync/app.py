"""Local review app. Nothing reaches the CRM unless a reviewer clicks Approve here.

Run:  BELLHAVEN_API_TOKEN=... python -m bellhaven_sync.app   ->  http://127.0.0.1:5000
"""
import traceback

from flask import Flask, redirect, render_template, request, url_for

from . import apply as applier
from . import pipeline, store
from .crm import CRMClient

app = Flask(__name__)

KIND_ORDER = ["reparent", "rename", "update", "duplicate", "create", "not_on_website", "reactivate"]


def db():
    return store.connect()


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
    p = store.get_proposal(conn, pid)
    if p is None or p["status"] not in (store.PENDING, store.APPROVED):
        return redirect(url_for("detail", pid=pid))
    note = request.form.get("note", "").strip() or None
    store.set_status(conn, pid, store.APPROVED, note=note)
    try:
        result = applier.apply_proposal(conn, CRMClient(), store.get_proposal(conn, pid))
        store.set_status(conn, pid, store.APPLIED, result=result)
    except applier.PreconditionFailed as exc:
        store.set_status(conn, pid, store.STALE, result={"error": str(exc)})
    except Exception as exc:  # keep it APPROVED so the reviewer can retry
        prior = store.get_proposal(conn, pid)["result"] or {}
        store.set_status(conn, pid, store.APPROVED, result={**prior, "error": str(exc), "trace": traceback.format_exc()})
    return redirect(url_for("detail", pid=pid))


@app.post("/proposal/<int:pid>/reject")
def reject(pid):
    conn = db()
    p = store.get_proposal(conn, pid)
    if p and p["status"] == store.PENDING:
        store.set_status(conn, pid, store.REJECTED, note=request.form.get("note", "").strip() or None)
    return redirect(request.form.get("next") or url_for("index"))


@app.post("/run")
def run_pipeline():
    pipeline.run()
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
