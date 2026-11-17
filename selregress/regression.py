"""回归判定、字段状态归并与运行摘要。"""

from __future__ import annotations

from typing import Any

from .models import BaselineDiff, FieldOutcome, Finding


def _worse(current: str, candidate: str) -> str:
    order = {"pass": 0, "info": 0, "warn": 1, "fail": 2}
    return candidate if order.get(candidate, 0) > order.get(current, 0) else current


def apply_statuses(
    outcomes: list[FieldOutcome], findings: list[Finding], diffs: list[BaselineDiff]
) -> None:
    for outcome in outcomes:
        outcome.status = "pass"
    for finding in findings:
        for outcome in outcomes:
            if (
                outcome.recipe_name == finding.recipe_name
                and outcome.field_name == finding.field_name
                and outcome.version == finding.version
                and outcome.snapshot_path == finding.snapshot_path
            ):
                outcome.status = _worse(outcome.status, finding.severity)
    for diff in diffs:
        for outcome in outcomes:
            if (
                outcome.recipe_name == diff.recipe_name
                and outcome.field_name == diff.field_name
                and outcome.snapshot_path == diff.snapshot_path
            ):
                outcome.status = _worse(outcome.status, diff.severity)


def decide_run_status(findings: list[Finding], diffs: list[BaselineDiff]) -> str:
    severities = [item.severity for item in findings] + [item.severity for item in diffs]
    if "fail" in severities:
        return "fail"
    if "warn" in severities:
        return "warn"
    return "pass"


def summarize(
    outcomes: list[FieldOutcome],
    findings: list[Finding],
    diffs: list[BaselineDiff],
    *,
    recipe_count: int,
    snapshot_count: int,
) -> tuple[str, dict[str, Any]]:
    apply_statuses(outcomes, findings, diffs)
    status = decide_run_status(findings, diffs)
    severity_counts = {"fail": 0, "warn": 0, "info": 0}
    for item in findings:
        severity_counts[item.severity] = severity_counts.get(item.severity, 0) + 1
    status_counts = {"pass": 0, "warn": 0, "fail": 0, "info": 0}
    for outcome in outcomes:
        status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
    scores = [outcome.score for outcome in outcomes]
    total = len(outcomes)
    with_value = sum(1 for item in outcomes if item.extraction.has_value)
    empty_count = sum(1 for item in outcomes if "empty_value" in item.flags)
    no_match_count = sum(1 for item in outcomes if "no_match" in item.flags)
    multi_count = sum(1 for item in outcomes if "multi_match" in item.flags)
    type_error_count = sum(1 for item in outcomes if "type_error" in item.flags)
    ambiguous_count = sum(1 for item in outcomes if "ambiguous_selector" in item.flags)
    drift_count = sum(1 for item in outcomes if "version_drift" in item.flags)

    def names(flag: str) -> list[str]:
        return sorted({f"{item.recipe_name}.{item.field_name}@{item.version}" for item in outcomes if flag in item.flags})

    summary = {
        "status": status,
        "recipe_count": recipe_count,
        "snapshot_count": snapshot_count,
        "field_result_count": len(outcomes),
        "finding_count": len(findings),
        "fail_finding_count": severity_counts.get("fail", 0),
        "warn_finding_count": severity_counts.get("warn", 0),
        "info_finding_count": severity_counts.get("info", 0),
        "baseline_diff_count": len(diffs),
        "baseline_fail_count": sum(1 for item in diffs if item.severity == "fail"),
        "baseline_warn_count": sum(1 for item in diffs if item.severity == "warn"),
        "field_pass_count": status_counts.get("pass", 0),
        "field_warn_count": status_counts.get("warn", 0),
        "field_fail_count": status_counts.get("fail", 0),
        "field_with_value_count": with_value,
        "hit_rate_percent": int(round(with_value * 100 / total)) if total else 100,
        "empty_field_count": empty_count,
        "empty_rate_percent": int(round(empty_count * 100 / total)) if total else 0,
        "no_match_field_count": no_match_count,
        "multi_match_field_count": multi_count,
        "type_error_field_count": type_error_count,
        "ambiguous_selector_count": ambiguous_count,
        "version_drift_field_count": drift_count,
        "empty_field_names": names("empty_value"),
        "no_match_field_names": names("no_match"),
        "multi_match_field_names": names("multi_match"),
        "type_error_field_names": names("type_error"),
        "ambiguous_field_names": names("ambiguous_selector"),
        "version_drift_field_names": names("version_drift"),
        "average_stability": int(round(sum(scores) / len(scores))) if scores else 100,
        "min_stability": min(scores) if scores else 100,
    }
    return status, summary
