"""SQLite 台账写入：单事务记录一次完整运行。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .ledger_query import LedgerQuery
from .ledger_schema import SCHEMA_STATEMENTS
from .models import RunResult
from .util import InputError, sha256_text


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return sha256_text(value)


class Ledger(LedgerQuery):
    def _write_connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        conn = self._write_connect()
        try:
            for statement in SCHEMA_STATEMENTS:
                conn.execute(statement)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(snapshot)")}
            if "captured_at" not in columns:
                conn.execute(
                    "ALTER TABLE snapshot ADD COLUMN captured_at TEXT NOT NULL DEFAULT ''"
                )
            conn.commit()
        finally:
            conn.close()

    def unique_run_id(self, base: str) -> str:
        self.initialize()
        conn = self._read_connect()
        try:
            candidate = base
            index = 2
            while conn.execute("SELECT 1 FROM run WHERE run_id = ?", (candidate,)).fetchone():
                candidate = f"{base}-{index}"
                index += 1
            return candidate
        finally:
            conn.close()

    def record_run(
        self, run: RunResult, report_dir: str, hashes: dict[str, str]
    ) -> int:
        conn = self._write_connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO run (
                        run_id, status, generated_at, fixed_time, config_path,
                        fail_on_warn, recipe_count, snapshot_count, field_result_count,
                        finding_count, baseline_diff_count, report_dir,
                        json_sha256, md_sha256, html_sha256
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.run_id, run.status, run.generated_at, int(run.fixed_time),
                        run.config_path, int(run.fail_on_warn), len(run.recipes),
                        len(run.snapshots), len(run.outcomes), len(run.findings),
                        len(run.diffs), report_dir, hashes.get("json", ""),
                        hashes.get("md", ""), hashes.get("html", ""),
                    ),
                )
                run_pk = int(cursor.lastrowid)
                for item in run.recipes:
                    conn.execute(
                        "INSERT INTO recipe (run_pk, name, site, page_type, path, sha256, baseline, field_count) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_pk, item["name"], item["site"], item["page_type"], item["path"],
                         item["sha256"], item.get("baseline"), item.get("field_count", 0)),
                    )
                for item in run.snapshots:
                    conn.execute(
                        "INSERT INTO snapshot (run_pk, site, page_type, version, path, sha256, size, captured_at, warnings_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (run_pk, item["site"], item["page_type"], item["version"], item["path"],
                         item["sha256"], item["size"], item.get("captured_at", ""),
                         _json(item.get("warnings", []))),
                    )
                for item in run.outcomes:
                    conn.execute(
                        """
                        INSERT INTO field_result (
                            run_pk, recipe_name, site, page_type, version, snapshot_path,
                            snapshot_sha256, field_name, field_type, required, multi,
                            used_selector, raw_values_json, normalized_value_json, match_count,
                            conversion_status, flags_json, score, status, error
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_pk, item.recipe_name, item.site, item.page_type, item.version,
                         item.snapshot_path, item.snapshot_sha256, item.field_name,
                         item.spec.field_type, int(item.spec.required), int(item.spec.multi),
                         item.extraction.used_selector, _json(item.extraction.raw_values),
                         _json(item.extraction.normalized_value), item.extraction.match_count,
                         item.extraction.conversion_status, _json(sorted(set(item.flags))),
                         item.score, item.status, item.extraction.error),
                    )
                for item in run.findings:
                    conn.execute(
                        """
                        INSERT INTO finding (
                            run_pk, code, severity, message, recipe_name, site, page_type,
                            version, snapshot_path, field_name, evidence_json, suggestions_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_pk, item.code, item.severity, item.message, item.recipe_name,
                         item.site, item.page_type, item.version, item.snapshot_path,
                         item.field_name, _json(item.evidence), _json(item.suggestions)),
                    )
                for item in run.diffs:
                    conn.execute(
                        """
                        INSERT INTO baseline_diff (
                            run_pk, change_type, severity, recipe_name, site, page_type,
                            field_name, old_value_json, new_value_json, message, snapshot_path
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_pk, item.change_type, item.severity, item.recipe_name,
                         item.site, item.page_type, item.field_name, _json(item.old_value),
                         _json(item.new_value), item.message, item.snapshot_path),
                    )
            return run_pk
        except sqlite3.IntegrityError as exc:
            raise InputError("LEDGER_WRITE_CONFLICT", f"台账写入冲突: {exc}") from exc
        finally:
            conn.close()

    def record_repair_plan(self, plan: dict) -> int:
        """记录一次只读修复预演。旧运行由 `record_run` 单独记录，二者通过 run_id 关联。"""

        plan_json = _json(plan)
        recommended = plan.get("recommended")
        conn = self._write_connect()
        try:
            with conn:
                cursor = conn.execute(
                    """
                    INSERT INTO repair_plan (
                        plan_id, base_run_id, recipe_name, scope,
                        target_fields_json, failing_fields_json, feasible,
                        replace_deliverable, recommended_json, conflicts_json,
                        plan_json, plan_sha256, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(plan.get("plan_id", "")),
                        str((plan.get("base_run") or {}).get("run_id", "")),
                        str((plan.get("recipe") or {}).get("name", "")),
                        str(plan.get("scope", "")),
                        _json(plan.get("requested_fields", [])),
                        _json(plan.get("failing_fields", [])),
                        int(bool(plan.get("feasible", False))),
                        int(bool(plan.get("replace_deliverable", False))),
                        _json(recommended),
                        _json(plan.get("conflicts", [])),
                        plan_json,
                        _sha256(plan_json),
                        str(plan.get("generated_at", "")),
                    ),
                )
                plan_pk = int(cursor.lastrowid)
                for item in plan.get("field_actions", []):
                    selectors = _json(item.get("selectors", []))
                    conn.execute(
                        """
                        INSERT INTO repair_action (
                            plan_pk, field_name, action, selectors_json,
                            added_selectors_json, candidate_selector,
                            target_scope_met, feasible_for_field,
                            conflicts_json, preview_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            plan_pk,
                            str(item.get("field_name", "")),
                            str(item.get("action", "")),
                            selectors,
                            _json(item.get("added_selectors", [])),
                            item.get("candidate_selector"),
                            int(bool(item.get("target_scope_met", False))),
                            int(bool(item.get("feasible_for_field", False))),
                            _json(item.get("conflicts", [])),
                            _json(item),
                        ),
                    )
            return plan_pk
        except sqlite3.IntegrityError as exc:
            raise InputError("LEDGER_WRITE_CONFLICT", f"计划写入冲突: {exc}") from exc
        finally:
            conn.close()
