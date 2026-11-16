"""JSON 基线读取与字段级差异对比。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import BaselineDiff, FieldOutcome, Recipe
from .util import InputError, PROJECT_ROOT, relative_posix, resolve_under_root

BASELINE_KEYS = {"schema_version", "site", "page_type", "version", "generated_at", "fields"}
BASELINE_FIELD_KEYS = {"normalized_value", "match_count", "conversion_status", "score"}


@dataclass(slots=True)
class Baseline:
    path: str
    site: str
    page_type: str
    version: str
    generated_at: str
    fields: dict[str, dict[str, Any]]


def load_baseline(path: str | Path, root: Path = PROJECT_ROOT) -> Baseline:
    baseline_path = resolve_under_root(path, root, "baseline")
    if not baseline_path.is_file():
        raise InputError("BASELINE_NOT_FOUND", f"基线文件不存在: {path}")
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError("BASELINE_INVALID", f"基线不是有效 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise InputError("BASELINE_INVALID", "基线根节点必须是对象")
    unknown = sorted(set(data) - BASELINE_KEYS)
    if unknown:
        raise InputError("BASELINE_INVALID", f"基线存在未知键: {', '.join(unknown)}")
    if data.get("schema_version") != 1:
        raise InputError("BASELINE_INVALID", "基线仅支持 schema_version: 1")
    fields = data.get("fields")
    if not isinstance(fields, dict) or not fields:
        raise InputError("BASELINE_INVALID", "基线 fields 必须是非空对象")
    normalized_fields: dict[str, dict[str, Any]] = {}
    for name, expected in fields.items():
        if not isinstance(expected, dict):
            raise InputError("BASELINE_INVALID", f"基线字段 {name} 必须是对象")
        unknown_field = sorted(set(expected) - BASELINE_FIELD_KEYS)
        if unknown_field:
            raise InputError("BASELINE_INVALID", f"基线字段 {name} 存在未知键")
        if "normalized_value" not in expected:
            raise InputError("BASELINE_INVALID", f"基线字段 {name} 缺少 normalized_value")
        normalized_fields[str(name)] = normalize_expected(expected)
    return Baseline(
        path=relative_posix(baseline_path, root),
        site=str(data.get("site", "")),
        page_type=str(data.get("page_type", "")),
        version=str(data.get("version", "")),
        generated_at=str(data.get("generated_at", "")),
        fields=normalized_fields,
    )


def normalize_expected(expected: dict[str, Any]) -> dict[str, Any]:
    score = expected.get("score", 100)
    if not isinstance(score, int) or isinstance(score, bool):
        score = 100
    count = expected.get("match_count", 1)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        count = 0
    return {
        "normalized_value": expected.get("normalized_value"),
        "match_count": count,
        "conversion_status": str(expected.get("conversion_status", "ok")),
        "score": max(0, min(100, score)),
    }


def _severity(value: str) -> str:
    return "info" if value in {"ignore", "pass"} else value


def _rule_severity(rules: dict[str, Any], key: str, default: str = "warn") -> str:
    return _severity(str(rules.get(key, default)))


def _severity_for_change(spec: Any, rules: dict[str, Any]) -> str:
    if spec is None:
        return _rule_severity(rules, "missing_field")
    value = spec.on_change if spec.on_change != "warn" else _rule_severity(rules, "default_on_change")
    return _severity(value)


def _diff(
    change_type: str,
    severity: str,
    recipe: Recipe,
    field_name: str,
    old_value: Any,
    new_value: Any,
    message: str,
    snapshot_path: str = "",
) -> BaselineDiff:
    return BaselineDiff(
        change_type=change_type, severity=severity, recipe_name=recipe.name,
        site=recipe.site, page_type=recipe.page_type, field_name=field_name,
        old_value=old_value, new_value=new_value, message=message,
        snapshot_path=snapshot_path,
    )


def latest_outcomes(outcomes: list[FieldOutcome]) -> dict[str, FieldOutcome]:
    latest: dict[str, FieldOutcome] = {}
    for outcome in outcomes:
        existing = latest.get(outcome.field_name)
        key = (outcome.version, outcome.snapshot_path)
        if existing is None or key > (existing.version, existing.snapshot_path):
            latest[outcome.field_name] = outcome
    return latest


def compare_baseline(
    recipe: Recipe,
    baseline: Baseline,
    outcomes: list[FieldOutcome],
    rules: dict[str, Any],
) -> list[BaselineDiff]:
    if baseline.site and baseline.site != recipe.site:
        raise InputError("BASELINE_SCOPE_MISMATCH", f"基线站点与配方不一致: {baseline.site}")
    if baseline.page_type and baseline.page_type != recipe.page_type:
        raise InputError("BASELINE_SCOPE_MISMATCH", f"基线页面类型与配方不一致: {baseline.page_type}")
    latest = latest_outcomes(outcomes)
    spec_by_name = {spec.name: spec for spec in recipe.fields}
    diffs: list[BaselineDiff] = []
    threshold = int(rules.get("stability_drop_threshold", 10))
    for field_name in sorted(set(baseline.fields) | set(latest)):
        expected = baseline.fields.get(field_name)
        current = latest.get(field_name)
        spec = spec_by_name.get(field_name)
        if expected is None and current is not None:
            diffs.append(_diff(
                "added_field", _rule_severity(rules, "added_field"), recipe, field_name,
                None, current.extraction.normalized_value,
                f"基线中没有字段 {field_name}，当前运行新增",
                current.snapshot_path,
            ))
            continue
        if current is None:
            severity = _rule_severity(rules, "missing_field")
            if spec is not None and spec.required:
                severity = _rule_severity(rules, "required_failure", "fail")
            diffs.append(_diff(
                "missing_field", severity, recipe, field_name,
                expected.get("normalized_value") if expected else None, None,
                f"当前运行缺少基线字段 {field_name}",
            ))
            continue
        assert expected is not None
        old_value = expected["normalized_value"]
        new_value = current.extraction.normalized_value
        if old_value != new_value:
            diffs.append(_diff(
                "value_changed", _severity_for_change(spec, rules), recipe, field_name,
                old_value, new_value, f"字段 {field_name} 归一化值发生变化",
                current.snapshot_path,
            ))
        if expected["match_count"] != current.extraction.match_count:
            diffs.append(_diff(
                "match_count_changed", _rule_severity(rules, "match_count_change"),
                recipe, field_name, expected["match_count"], current.extraction.match_count,
                f"字段 {field_name} 匹配数量发生变化", current.snapshot_path,
            ))
        if expected["conversion_status"] != current.extraction.conversion_status:
            diffs.append(_diff(
                "type_status_changed", _rule_severity(rules, "type_status_change"),
                recipe, field_name, expected["conversion_status"],
                current.extraction.conversion_status,
                f"字段 {field_name} 类型转换状态发生变化", current.snapshot_path,
            ))
        drop = expected["score"] - current.score
        if drop != 0:
            severity = _rule_severity(rules, "stability_change") if drop > threshold else "info"
            direction = f"下降 {drop} 分" if drop > 0 else f"上升 {-drop} 分"
            diffs.append(_diff(
                "stability_changed", severity, recipe, field_name,
                expected["score"], current.score,
                f"字段 {field_name} 稳定性评分{direction}", current.snapshot_path,
            ))
    diffs.sort(key=lambda item: (item.field_name, item.change_type, item.snapshot_path))
    return diffs
