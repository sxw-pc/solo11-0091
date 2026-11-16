import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from selregress.ledger import Ledger
from selregress.runner import execute
from selregress.util import PROJECT_ROOT, InputError

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def test_schema_contains_required_tables(shared_execution):
    ledger = Ledger(_shared_db(), PROJECT_ROOT)
    ledger.initialize()
    with sqlite3.connect(ledger.path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"run", "recipe", "snapshot", "field_result", "finding", "baseline_diff"} <= tables


def _shared_db():
    return TMP_ROOT / "shared" / "ledger.db"


def test_query_fields_with_filters(shared_execution):
    ledger = Ledger(_shared_db(), PROJECT_ROOT)
    run_id = shared_execution.result.run_id
    fields = ledger.query_fields(run_id, site="site-a.invalid", page_type="detail", field_name="price")
    assert len(fields) == 2
    assert {item["version"] for item in fields} == {"2024-01", "2024-02"}
    warned = ledger.query_fields(run_id, severity="warn")
    assert warned
    assert all(item["status"] == "warn" for item in warned)


def test_query_findings_and_diffs(shared_execution):
    ledger = Ledger(_shared_db(), PROJECT_ROOT)
    run_id = shared_execution.result.run_id
    findings = ledger.query_findings(run_id, severity="warn")
    assert len(findings) == 11
    assert findings[0]["severity"] == "warn"
    assert all(isinstance(item["evidence"], dict) for item in findings)
    assert ledger.query_findings(run_id, field_name="title")[0]["code"] == "ambiguous_selector"
    diffs = ledger.query_diffs(run_id)
    assert any(item["change_type"] == "value_changed" for item in diffs)


def test_recipe_snapshot_and_detail_rows(shared_execution):
    ledger = Ledger(_shared_db(), PROJECT_ROOT)
    run = ledger.get_run(shared_execution.result.run_id)
    assert run is not None
    recipes = ledger.list_recipes(run["id"])
    snapshots = ledger.list_snapshots(run["id"])
    assert len(recipes) == 3
    assert len(snapshots) == 6
    snapshot = ledger.get_snapshot(snapshots[0]["id"])
    assert snapshot["path"].startswith("fixtures/snapshots/")
    finding = ledger.query_findings(shared_execution.result.run_id)[0]
    assert ledger.get_finding(finding["id"])["message"] == finding["message"]


def test_duplicate_run_insert_does_not_overwrite(shared_execution):
    ledger = Ledger(_shared_db(), PROJECT_ROOT)
    with sqlite3.connect(ledger.path) as conn:
        before = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    duplicate = replace(shared_execution.result, run_id=shared_execution.result.run_id)
    with pytest.raises(InputError) as caught:
        ledger.record_run(duplicate, duplicate.report_dir, duplicate.report_hashes)
    assert caught.value.code == "LEDGER_WRITE_CONFLICT"
    with sqlite3.connect(ledger.path) as conn:
        after = conn.execute("SELECT COUNT(*) FROM run").fetchone()[0]
    assert before == after


def test_second_run_keeps_history(shared_config, workspace):
    db = workspace / "ledger.db"
    runs = workspace / "runs"
    first = execute(shared_config, ledger_path=db, runs_dir=runs)
    second = execute(shared_config, ledger_path=db, runs_dir=runs)
    assert first.result.run_id != second.result.run_id
    assert second.result.run_id.endswith("-2")
    ledger = Ledger(db, PROJECT_ROOT)
    assert len(ledger.list_runs(limit=10)) == 2
    assert (PROJECT_ROOT / first.result.report_dir / "report.json").is_file()
    export = ledger.export_latest()
    assert export["run"]["run_id"] == second.result.run_id
