"""字段级匹配质量诊断、稳定性评分与跨版本漂移。"""

from __future__ import annotations

from typing import Any

from .models import Extraction, FieldOutcome, Finding, NodeSelection, Recipe, SnapshotMeta
from .suggest import analyze_failure
from .util import clamp_score

PENALTIES = {
    "no_match": (-40, -25),
    "empty_value": (-30, -30),
    "multi_match": (-20, -20),
    "type_error": (-25, -25),
    "ambiguous_selector": (-15, -15),
    "version_drift": (-15, -15),
}


def _severity(rules: dict[str, Any], code: str, required: bool) -> str:
    if required and code in {"no_match", "empty_value", "type_error"}:
        value = str(rules.get("required_failure", "fail"))
    else:
        value = str(rules.get(code, "warn"))
    return "info" if value in {"pass", "ignore"} else value


def _finding(
    *,
    code: str,
    severity: str,
    message: str,
    recipe: Recipe,
    snapshot: SnapshotMeta,
    field_name: str,
    evidence: dict[str, Any] | None = None,
    suggestions: list[dict[str, Any]] | None = None,
) -> Finding:
    return Finding(
        code=code,
        severity=severity,
        message=message,
        recipe_name=recipe.name,
        site=recipe.site,
        page_type=recipe.page_type,
        version=snapshot.version,
        snapshot_path=snapshot.path,
        field_name=field_name,
        evidence=evidence or {},
        suggestions=suggestions or [],
    )


def _score(flags: list[str], required: bool) -> tuple[int, list[str]]:
    score = 100
    reasons: list[str] = []
    labels = {
        "no_match": "选择器无命中",
        "empty_value": "命中但值为空",
        "multi_match": "单值字段匹配到多个节点",
        "type_error": "类型转换失败",
        "ambiguous_selector": "多候选选择器结果不一致",
        "version_drift": "跨版本值有无状态漂移",
    }
    for flag in sorted(set(flags)):
        if flag not in PENALTIES:
            continue
        required_penalty, optional_penalty = PENALTIES[flag]
        penalty = required_penalty if required and flag in {"no_match", "empty_value", "type_error"} else optional_penalty
        score += penalty
        reasons.append(f"{labels[flag]}（{penalty}）")
    return clamp_score(score), reasons


def diagnose_field(
    *,
    recipe: Recipe,
    snapshot: SnapshotMeta,
    spec: Any,
    selection: NodeSelection,
    extraction: Extraction,
    soup: Any,
    rules: dict[str, Any],
) -> tuple[FieldOutcome, list[Finding]]:
    flags: list[str] = []
    findings: list[Finding] = []
    if selection.count == 0:
        flags.append("no_match")
        evidence, suggestions = analyze_failure(soup, spec, selection)
        severity = _severity(rules, "no_match", spec.required)
        findings.append(_finding(
            code="no_match", severity=severity,
            message=f"字段 {spec.name} 的选择器没有任何命中",
            recipe=recipe, snapshot=snapshot, field_name=spec.name,
            evidence=evidence, suggestions=suggestions,
        ))
    elif extraction.conversion_status == "empty":
        flags.append("empty_value")
        severity = _severity(rules, "empty_value", spec.required)
        findings.append(_finding(
            code="empty_value", severity=severity,
            message=f"字段 {spec.name} 命中节点但归一化后为空",
            recipe=recipe, snapshot=snapshot, field_name=spec.name,
            evidence={"raw_values": extraction.raw_values, "used_selector": extraction.used_selector},
        ))
    if not spec.multi and selection.count > 1:
        flags.append("multi_match")
        evidence, suggestions = analyze_failure(soup, spec, selection)
        severity = _severity(rules, "multi_match", spec.required)
        findings.append(_finding(
            code="multi_match", severity=severity,
            message=f"单值字段 {spec.name} 命中 {selection.count} 个节点",
            recipe=recipe, snapshot=snapshot, field_name=spec.name,
            evidence=evidence, suggestions=suggestions,
        ))
    if extraction.conversion_status == "type_error":
        flags.append("type_error")
        severity = _severity(rules, "type_error", spec.required)
        findings.append(_finding(
            code="type_error", severity=severity,
            message=f"字段 {spec.name} 类型转换失败，已保留原始值",
            recipe=recipe, snapshot=snapshot, field_name=spec.name,
            evidence={
                "error": extraction.error,
                "raw_values": extraction.raw_values,
                "used_selector": extraction.used_selector,
            },
        ))
    if extraction.ambiguous:
        flags.append("ambiguous_selector")
        severity = _severity(rules, "ambiguous_selector", spec.required)
        findings.append(_finding(
            code="ambiguous_selector", severity=severity,
            message=f"字段 {spec.name} 的多个候选选择器结果不一致",
            recipe=recipe, snapshot=snapshot, field_name=spec.name,
            evidence={
                "detail": extraction.ambiguity_detail,
                "selector_hits": extraction.selector_hits,
                "used_selector": extraction.used_selector,
            },
        ))
    score, reasons = _score(flags, spec.required)
    outcome = FieldOutcome(
        recipe_name=recipe.name, recipe_path=recipe.path, site=recipe.site,
        page_type=recipe.page_type, version=snapshot.version,
        snapshot_path=snapshot.path, snapshot_sha256=snapshot.sha256,
        spec=spec, extraction=extraction, flags=flags, score=score,
        score_reasons=reasons,
        status="fail" if any(item.severity == "fail" for item in findings)
        else ("warn" if findings else "pass"),
    )
    return outcome, findings


def apply_version_drift(recipe: Recipe, outcomes: list[FieldOutcome], rules: dict[str, Any]) -> list[Finding]:
    findings: list[Finding] = []
    by_field: dict[str, list[FieldOutcome]] = {}
    for outcome in outcomes:
        by_field.setdefault(outcome.field_name, []).append(outcome)
    for field_name in sorted(by_field):
        versions = sorted(by_field[field_name], key=lambda item: (item.version, item.snapshot_path))
        for previous, current in zip(versions, versions[1:]):
            before = previous.extraction.has_value
            after = current.extraction.has_value
            if before == after:
                continue
            current.flags.append("version_drift")
            current.flags = sorted(set(current.flags))
            current.score = clamp_score(current.score - 15)
            current.score_reasons.append("跨版本值有无状态漂移（-15）")
            severity = _severity(rules, "version_drift", False)
            if recipe.fields and current.spec.required and not after:
                severity = str(rules.get("required_failure", "fail"))
            if severity == "fail":
                current.status = "fail"
            elif severity == "warn" and current.status == "pass":
                current.status = "warn"
            findings.append(Finding(
                code="version_drift", severity=severity,
                message=f"字段 {field_name} 在 {previous.version} → {current.version} 发生值有无漂移",
                recipe_name=recipe.name, site=recipe.site, page_type=recipe.page_type,
                version=current.version, snapshot_path=current.snapshot_path,
                field_name=field_name,
                evidence={
                    "field_path": f"{recipe.site}.{recipe.page_type}.{field_name}",
                    "version_pair": [previous.version, current.version],
                    "before_has_value": before,
                    "after_has_value": after,
                    "before_value": previous.extraction.normalized_value,
                    "after_value": current.extraction.normalized_value,
                },
            ))
    return findings
