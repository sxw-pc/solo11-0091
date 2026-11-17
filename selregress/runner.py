"""一次回归运行的编排：加载 → 提取 → 诊断 → 回归 → 报告 → 台账。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .baseline import compare_baseline, load_baseline
from .config import AppConfig
from .diagnostics import apply_version_drift, diagnose_field
from .extractor import extract_field
from .ledger import Ledger
from .loader import InputBundle, load_bundle
from .matcher import match_nodes
from .models import BaselineDiff, FieldOutcome, Finding, RunResult
from .regression import summarize
from .reporting import write_reports
from .util import canonical_digest, normalized_fixed_time, run_stamp, stable_json_text


@dataclass(slots=True)
class Execution:
    result: RunResult
    artifacts: dict[str, Any]
    warnings: list[str] = field(default_factory=list)


def _fingerprint(
    config: AppConfig, bundle: InputBundle, generated_at: str, fail_on_warn: bool
) -> str:
    payload = {
        "config": config.name,
        "generated_at": generated_at,
        "fail_on_warn": fail_on_warn,
        "rules": config.rules,
        "recipes": [
            {"name": item.name, "sha256": item.sha256, "path": item.path}
            for item in bundle.recipes
        ],
        "snapshots": [
            {"path": item.meta.path, "sha256": item.meta.sha256}
            for item in bundle.snapshots
        ],
    }
    return canonical_digest(payload)[:12]


def execute(
    config: AppConfig,
    *,
    fixed_time: str | None = None,
    ledger_path: Path | None = None,
    runs_dir: Path | None = None,
    fail_on_warn: bool = False,
) -> Execution:
    bundle = load_bundle(config)
    generated_at, is_fixed = normalized_fixed_time(
        fixed_time if fixed_time is not None else config.fixed_time
    )
    rules = dict(config.rules)
    outcomes: list[FieldOutcome] = []
    findings: list[Finding] = []
    diffs: list[BaselineDiff] = []
    outcomes_by_recipe: dict[str, list[FieldOutcome]] = {}
    for recipe in bundle.recipes:
        matching = [
            item for item in bundle.snapshots
            if item.meta.site == recipe.site and item.meta.page_type == recipe.page_type
        ]
        recipe_outcomes: list[FieldOutcome] = []
        for loaded in matching:
            for spec in recipe.fields:
                selection = match_nodes(loaded.soup, spec)
                extraction = extract_field(selection, spec)
                outcome, field_findings = diagnose_field(
                    recipe=recipe, snapshot=loaded.meta, spec=spec,
                    selection=selection, extraction=extraction, soup=loaded.soup,
                    rules=rules,
                )
                recipe_outcomes.append(outcome)
                findings.extend(field_findings)
        findings.extend(apply_version_drift(recipe, recipe_outcomes, rules))
        outcomes_by_recipe[recipe.name] = recipe_outcomes
        outcomes.extend(recipe_outcomes)
        if recipe.baseline:
            baseline = load_baseline(recipe.baseline, config.root)
            diffs.extend(compare_baseline(recipe, baseline, recipe_outcomes, rules))
    outcomes.sort(key=lambda item: item.sort_key)
    findings.sort(key=lambda item: (
        item.recipe_name, item.site, item.page_type, item.version, item.snapshot_path,
        item.field_name or "", item.code, item.severity, item.message,
    ))
    diffs.sort(key=lambda item: (
        item.recipe_name, item.site, item.page_type, item.field_name,
        item.change_type, item.snapshot_path,
    ))
    status, summary = summarize(
        outcomes, findings, diffs,
        recipe_count=len(bundle.recipes), snapshot_count=len(bundle.snapshots),
    )
    summary["input_warning_count"] = len(bundle.warnings)
    digest = _fingerprint(config, bundle, generated_at, fail_on_warn)
    ledger = Ledger(ledger_path or config.ledger_path, config.root)
    run_id = ledger.unique_run_id(f"{run_stamp(generated_at)}-{digest}")
    result = RunResult(
        run_id=run_id,
        status=status,
        generated_at=generated_at,
        fixed_time=is_fixed,
        config_path=config.config_relpath,
        fail_on_warn=fail_on_warn,
        recipes=[item.to_meta() for item in bundle.recipes],
        snapshots=[item.meta.to_meta() for item in bundle.snapshots],
        outcomes=outcomes,
        findings=findings,
        diffs=diffs,
        summary=summary,
    )
    artifacts = write_reports(result, runs_dir or config.runs_dir, config.root)
    ledger.record_run(result, result.report_dir, result.report_hashes)
    return Execution(result=result, artifacts=artifacts, warnings=bundle.warnings)


def execution_summary(execution: Execution) -> dict[str, Any]:
    result = execution.result
    return {
        "run_id": result.run_id,
        "status": result.status,
        "exit_code": result.exit_code,
        "generated_at": result.generated_at,
        "fixed_time": result.fixed_time,
        "summary": result.summary,
        "warnings": execution.warnings,
        "reports": execution.artifacts["files"],
        "report_hashes": result.report_hashes,
    }


def dump_execution_json(execution: Execution) -> str:
    return stable_json_text(execution_summary(execution))
