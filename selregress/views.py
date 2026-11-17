"""复核台 GET 路由。所有动态值由 Jinja 自动转义。"""

from __future__ import annotations

import json
from typing import Any

from flask import Flask, abort, render_template, request

from .ledger_query import LedgerQuery
from .util import InputError


def _record_or_404(record: dict[str, Any] | None, label: str) -> dict[str, Any]:
    if record is None:
        abort(404, description=f"{label}不存在")
    return record


def register_routes(app: Flask, ledger: LedgerQuery) -> None:
    @app.errorhandler(404)
    def not_found(error):  # type: ignore[no-untyped-def]
        return render_template("error.html", code=404, title="页面不存在",
                               message=getattr(error, "description", "页面不存在")), 404

    @app.errorhandler(405)
    def method_not_allowed(error):  # type: ignore[no-untyped-def]
        return render_template("error.html", code=405, title="只读复核台",
                               message="当前复核台只接受 GET/HEAD 请求。"), 405

    @app.errorhandler(503)
    def unavailable(error):  # type: ignore[no-untyped-def]
        return render_template("error.html", code=503, title="台账不可用",
                               message="请先运行一次 demo 或 run 以生成台账。"), 503

    @app.route("/")
    def index():  # type: ignore[no-untyped-def]
        site = request.args.get("site") or None
        status = request.args.get("status") or None
        try:
            runs = ledger.list_runs(limit=100, site=site, status=status)
            latest = ledger.latest_run()
        except InputError:
            abort(503)
        return render_template("index.html", runs=runs, latest=latest,
                               filter_site=site or "", filter_status=status or "")

    @app.route("/runs/<run_id>")
    def run_detail(run_id: str):  # type: ignore[no-untyped-def]
        try:
            run = _record_or_404(ledger.get_run(run_id), "运行")
            run_pk = int(run["id"])
            recipes = ledger.list_recipes(run_pk)
            snapshots = ledger.list_snapshots(run_pk)
            filters = {
                "site": request.args.get("site") or None,
                "page_type": request.args.get("page_type") or None,
                "field_name": request.args.get("field_name") or None,
                "severity": request.args.get("severity") or None,
            }
            fields = ledger.query_fields(run_id, **filters)
            snapshot_ids = {item["path"]: item["id"] for item in snapshots}
            for item in fields:
                item["snapshot_id"] = snapshot_ids.get(item["snapshot_path"])
            findings = ledger.query_findings(run_id)
            diffs = ledger.query_diffs(run_id)
        except InputError:
            abort(503)
        return render_template(
            "run_detail.html", run=run, recipes=recipes, snapshots=snapshots,
            fields=fields, findings=findings, diffs=diffs, filters=filters,
        )

    @app.route("/snapshots/<int:snapshot_id>")
    def snapshot_detail(snapshot_id: int):  # type: ignore[no-untyped-def]
        try:
            snapshot = _record_or_404(ledger.get_snapshot(snapshot_id), "快照记录")
            run = _record_or_404(ledger.get_run_by_pk(int(snapshot["run_pk"])), "运行")
            fields = [
                item for item in ledger.query_fields(str(run["run_id"]))
                if item["snapshot_path"] == snapshot["path"]
            ]
        except InputError:
            abort(503)
        return render_template("snapshot_detail.html", snapshot=snapshot, run=run, fields=fields)

    @app.route("/findings/<int:finding_id>")
    def finding_detail(finding_id: int):  # type: ignore[no-untyped-def]
        try:
            finding = _record_or_404(ledger.get_finding(finding_id), "发现项")
            run = _record_or_404(ledger.get_run_by_pk(int(finding["run_pk"])), "运行")
        except InputError:
            abort(503)
        return render_template(
            "finding_detail.html", finding=finding, run=run,
            evidence_json=json.dumps(finding["evidence"], ensure_ascii=False, indent=2, sort_keys=True),
        )
