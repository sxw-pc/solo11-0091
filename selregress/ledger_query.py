"""台账只读查询。复核台只依赖本模块的查询能力。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .util import InputError, PROJECT_ROOT, resolve_under_root


def _loads(value: str | None, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


class LedgerQuery:
    def __init__(self, path: Path, root: Path = PROJECT_ROOT):
        self.path = resolve_under_root(path, root, "ledger_path")
        self.root = root

    def _read_connect(self) -> sqlite3.Connection:
        if not self.path.is_file():
            raise InputError("LEDGER_NOT_FOUND", f"台账不存在: {self.path}")
        conn = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            return _row(conn.execute("SELECT * FROM run WHERE run_id = ?", (run_id,)).fetchone())
        finally:
            conn.close()

    def get_run_by_pk(self, run_pk: int) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            return _row(conn.execute("SELECT * FROM run WHERE id = ?", (run_pk,)).fetchone())
        finally:
            conn.close()

    def latest_run(self) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            return _row(conn.execute("SELECT * FROM run ORDER BY id DESC LIMIT 1").fetchone())
        finally:
            conn.close()

    def list_runs(
        self, limit: int = 50, site: str | None = None, status: str | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if site:
            clauses.append("EXISTS (SELECT 1 FROM snapshot s WHERE s.run_pk = run.id AND s.site = ?)")
            params.append(site)
        if status:
            clauses.append("run.status = ?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        params.append(max(1, min(limit, 500)))
        conn = self._read_connect()
        try:
            rows = conn.execute(
                f"SELECT run.* FROM run{where} ORDER BY run.id DESC LIMIT ?", params
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def _run_pk(self, run_id: str) -> int:
        record = self.get_run(run_id)
        if record is None:
            raise InputError("RUN_NOT_FOUND", f"运行不存在: {run_id}")
        return int(record["id"])

    def list_recipes(self, run_pk: int) -> list[dict[str, Any]]:
        conn = self._read_connect()
        try:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM recipe WHERE run_pk = ? ORDER BY name", (run_pk,))]
        finally:
            conn.close()

    def list_snapshots(self, run_pk: int) -> list[dict[str, Any]]:
        conn = self._read_connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM snapshot WHERE run_pk = ? ORDER BY site, page_type, version, path",
                (run_pk,))]
            for row in rows:
                row["warnings"] = _loads(row.pop("warnings_json"), [])
            return rows
        finally:
            conn.close()

    def query_fields(
        self, run_id: str, *, site: str | None = None, page_type: str | None = None,
        field_name: str | None = None, severity: str | None = None,
    ) -> list[dict[str, Any]]:
        run_pk = self._run_pk(run_id)
        clauses = ["run_pk = ?"]
        params: list[Any] = [run_pk]
        for column, value in (("site", site), ("page_type", page_type),
                              ("field_name", field_name), ("status", severity)):
            if value:
                clauses.append(f"{column} = ?")
                params.append(value)
        conn = self._read_connect()
        try:
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM field_result WHERE {' AND '.join(clauses)} "
                "ORDER BY recipe_name, site, page_type, version, snapshot_path, field_name",
                params)]
            for row in rows:
                row["raw_values"] = _loads(row.pop("raw_values_json"), [])
                row["normalized_value"] = _loads(row.pop("normalized_value_json"), None)
                row["flags"] = _loads(row.pop("flags_json"), [])
                row["required"] = bool(row["required"])
                row["multi"] = bool(row["multi"])
            return rows
        finally:
            conn.close()

    def query_findings(
        self, run_id: str, *, severity: str | None = None, field_name: str | None = None
    ) -> list[dict[str, Any]]:
        run_pk = self._run_pk(run_id)
        clauses = ["run_pk = ?"]
        params: list[Any] = [run_pk]
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if field_name:
            clauses.append("field_name = ?")
            params.append(field_name)
        conn = self._read_connect()
        try:
            rows = [dict(row) for row in conn.execute(
                f"SELECT * FROM finding WHERE {' AND '.join(clauses)} "
                "ORDER BY CASE severity WHEN 'fail' THEN 0 WHEN 'warn' THEN 1 ELSE 2 END, "
                "code, recipe_name, snapshot_path, field_name, id", params)]
            for row in rows:
                row["evidence"] = _loads(row.pop("evidence_json"), {})
                row["suggestions"] = _loads(row.pop("suggestions_json"), [])
            return rows
        finally:
            conn.close()

    def query_diffs(self, run_id: str) -> list[dict[str, Any]]:
        run_pk = self._run_pk(run_id)
        conn = self._read_connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM baseline_diff WHERE run_pk = ? "
                "ORDER BY field_name, change_type, snapshot_path", (run_pk,))]
            for row in rows:
                row["old_value"] = _loads(row.pop("old_value_json"), None)
                row["new_value"] = _loads(row.pop("new_value_json"), None)
            return rows
        finally:
            conn.close()

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            row = _row(conn.execute("SELECT * FROM snapshot WHERE id = ?", (snapshot_id,)).fetchone())
            if row:
                row["warnings"] = _loads(row.pop("warnings_json"), [])
            return row
        finally:
            conn.close()

    def get_finding(self, finding_id: int) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            row = _row(conn.execute("SELECT * FROM finding WHERE id = ?", (finding_id,)).fetchone())
            if row:
                row["evidence"] = _loads(row.pop("evidence_json"), {})
                row["suggestions"] = _loads(row.pop("suggestions_json"), [])
            return row
        finally:
            conn.close()

    def health(self) -> dict[str, Any]:
        conn = self._read_connect()
        try:
            existing = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            counts = {}
            for table in (
                "run", "recipe", "snapshot", "field_result", "finding",
                "baseline_diff", "repair_plan", "repair_action",
            ):
                if table not in existing:
                    counts[table] = 0
                    continue
                counts[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            return {"status": "ok", "database": self.path.name, "tables": counts}
        finally:
            conn.close()

    def list_repair_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        conn = self._read_connect()
        try:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM repair_plan ORDER BY id DESC LIMIT ?",
                    (max(1, min(limit, 500)),),
                ).fetchall()
            ]
            for row in rows:
                row["target_fields"] = _loads(row.pop("target_fields_json"), [])
                row["failing_fields"] = _loads(row.pop("failing_fields_json"), [])
                row["recommended"] = _loads(row.pop("recommended_json"), None)
                row["conflicts"] = _loads(row.pop("conflicts_json"), [])
                row["feasible"] = bool(row["feasible"])
                row["replace_deliverable"] = bool(row["replace_deliverable"])
            return rows
        finally:
            conn.close()

    def get_repair_plan(self, plan_id: str, base_run_id: str | None = None) -> dict[str, Any] | None:
        conn = self._read_connect()
        try:
            if base_run_id is None:
                row = conn.execute(
                    "SELECT * FROM repair_plan WHERE plan_id = ? ORDER BY id DESC LIMIT 1",
                    (plan_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM repair_plan WHERE plan_id = ? AND base_run_id = ? "
                    "ORDER BY id DESC LIMIT 1",
                    (plan_id, base_run_id),
                ).fetchone()
            if row is None:
                return None
            record = dict(row)
            record["target_fields"] = _loads(record.pop("target_fields_json"), [])
            record["failing_fields"] = _loads(record.pop("failing_fields_json"), [])
            record["recommended"] = _loads(record.pop("recommended_json"), None)
            record["conflicts"] = _loads(record.pop("conflicts_json"), [])
            record["plan"] = _loads(record.pop("plan_json"), {})
            record["feasible"] = bool(record["feasible"])
            record["replace_deliverable"] = bool(record["replace_deliverable"])
            return record
        finally:
            conn.close()

    def query_repair_actions(self, plan_id: str) -> list[dict[str, Any]]:
        conn = self._read_connect()
        try:
            rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT a.* FROM repair_action a "
                    "JOIN repair_plan p ON p.id = a.plan_pk "
                    "WHERE p.plan_id = ? ORDER BY a.field_name, a.action, a.selectors_json, a.id",
                    (plan_id,),
                ).fetchall()
            ]
            for row in rows:
                row["selectors"] = _loads(row.pop("selectors_json"), [])
                row["added_selectors"] = _loads(row.pop("added_selectors_json"), [])
                row["conflicts"] = _loads(row.pop("conflicts_json"), [])
                row["preview"] = _loads(row.pop("preview_json"), {})
                row["target_scope_met"] = bool(row["target_scope_met"])
                row["feasible_for_field"] = bool(row["feasible_for_field"])
            return rows
        finally:
            conn.close()

    def export_latest(self) -> dict[str, Any]:
        record = self.latest_run()
        if record is None:
            return {"status": "empty", "run": None}
        return {
            "status": "ok",
            "run": {
                "run_id": record["run_id"],
                "status": record["status"],
                "generated_at": record["generated_at"],
                "recipe_count": record["recipe_count"],
                "snapshot_count": record["snapshot_count"],
                "field_result_count": record["field_result_count"],
                "finding_count": record["finding_count"],
                "baseline_diff_count": record["baseline_diff_count"],
                "report_dir": record["report_dir"],
            },
        }
