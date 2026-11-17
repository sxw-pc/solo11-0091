"""只读选择器修复预演。

本模块在内存中构造候选选择器动作（替换主选择器 / 追加 fallback / 放弃），对每种
动作真实重跑 `matcher.match_nodes` → `extractor.extract_field` →
`diagnostics.diagnose_field`，再经 `regression.summarize` 与基线规则计算修改后的
状态。它不会写回 YAML，也不会改动快照；唯一写盘行为是运行台账与预演报告。

判定原则：
- 建议置信分只是线索，绝不当作修复成功；成功只由真实抽取结果决定。
- `current` 只承诺最新快照的抽取；`replay` 承诺全部归档版本仍可回放。
- 未列为目标的字段结果必须逐项保持一致，否则方案不可交付。
- 计划级择优顺序：可行优先，其后按改动字段数、新增候选数、稳定字段/选择器顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import product
from typing import Any, Iterable, Sequence

from .baseline import Baseline, compare_baseline, load_baseline
from .config import AppConfig
from .diagnostics import apply_version_drift, diagnose_recipe_snapshot, outcome_is_clean
from .ledger import Ledger
from .loader import InputBundle, load_bundle
from .matcher import match_nodes
from .models import BaselineDiff, FieldOutcome, FieldSpec, Finding, Recipe
from .regression import (
    field_scope_clean,
    outcome_fingerprint,
    outcomes_equal,
    summarize,
)
from .reporting import write_repair_plan_reports
from .runner import execute
from .snapshot_reader import LoadedSnapshot
from .suggest import enumerate_repair_candidates
from .util import InputError, canonical_digest, truncate

SCOPES = ("current", "replay")
ACTION_ORDER = {"replace": 0, "append": 1, "abandon": 2}
PLAN_SCHEMA_VERSION = 1
MAX_CANDIDATES_PER_FIELD = 10
MAX_ACTIONS_PER_FIELD = 4
MAX_PLANS = 256
MAX_CONFLICTS = 80


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldAction:
    field_name: str
    action: str
    selectors: tuple[str, ...]
    added_selectors: tuple[str, ...]
    candidate_selector: str | None
    source: str
    suggestion_score: int
    suggestion_confidence: str
    suggestion_reason: str

    @property
    def rank(self) -> tuple[Any, ...]:
        return (
            ACTION_ORDER.get(self.action, 9),
            -self.suggestion_score,
            self.candidate_selector or "",
            self.selectors,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "field_name": self.field_name,
            "action": self.action,
            "selectors": list(self.selectors),
            "added_selectors": list(self.added_selectors),
            "candidate_selector": self.candidate_selector,
            "source": self.source,
            "suggestion": {
                "score": self.suggestion_score,
                "confidence": self.suggestion_confidence,
                "reason": self.suggestion_reason,
                "advisory_only": True,
                "confidence_is_not_repair_evidence": True,
            },
        }


@dataclass(slots=True)
class Rehearsal:
    recipes: dict[str, Recipe]
    outcomes: list[FieldOutcome]
    findings: list[Finding]
    diffs: list[BaselineDiff]
    status: str
    summary: dict[str, Any]
    outcome_map: dict[tuple[str, str, str, str], FieldOutcome]


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------


def _outcome_key(outcome: FieldOutcome) -> tuple[str, str, str, str]:
    return (
        outcome.recipe_name,
        outcome.version,
        outcome.snapshot_path,
        outcome.field_name,
    )


def _conflict(
    kind: str,
    *,
    field_name: str | None,
    message: str,
    version: str | None = None,
    snapshot_path: str | None = None,
    selector: str | None = None,
    expected: Any = None,
    actual: Any = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "field_name": field_name,
        "version": version,
        "snapshot_path": snapshot_path,
        "selector": selector,
        "message": message,
        "expected": expected,
        "actual": actual,
        "evidence": evidence or {},
    }


def _conflict_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(item.get("kind", "")),
        str(item.get("field_name") or ""),
        str(item.get("version") or ""),
        str(item.get("snapshot_path") or ""),
        str(item.get("selector") or ""),
        str(item.get("message", "")),
    )


def _dedupe_conflicts(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []
    for item in sorted(items, key=_conflict_sort_key):
        signature = canonical_digest(item)
        if signature in seen:
            continue
        seen.add(signature)
        output.append(item)
        if len(output) >= MAX_CONFLICTS:
            break
    return output


def _classes(node: Any) -> list[str]:
    value = node.get("class", []) if node is not None else []
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value]


def _node_related(left: Any, right: Any) -> str | None:
    if left is right:
        return "same_node"
    left_ancestors = {id(node) for node in getattr(left, "parents", [])}
    right_ancestors = {id(node) for node in getattr(right, "parents", [])}
    if id(right) in left_ancestors:
        return "candidate_is_ancestor"
    if id(left) in right_ancestors:
        return "candidate_is_descendant"
    return None


def _node_label(node: Any) -> dict[str, Any]:
    if node is None:
        return {}
    return {
        "tag": getattr(node, "name", None),
        "classes": sorted(_classes(node)),
        "text": truncate(node.get_text(" ", strip=True), 100),
    }


def _is_record_like(node: Any) -> bool:
    if node is None or not getattr(node, "name", None):
        return False
    classes = set(_classes(node))
    return node.name in {"article", "li", "tr"} or bool(
        classes & {"card", "item", "product", "record", "entry"}
    )


def _record_ancestor(node: Any) -> Any:
    current = getattr(node, "parent", None)
    depth = 0
    while current is not None and getattr(current, "name", None) and current.name != "[document]":
        if _is_record_like(current):
            return current
        current = current.parent
        depth += 1
        if depth > 6:
            break
    return getattr(node, "parent", None)


def _record_index(ancestor: Any) -> int | None:
    """记录节点在同级记录中的位置，用于比较“是否同一业务对象”。

    版本改版常伴随标题文案变化，因此不能只比较标题；同一单卡片页面的记录位次
    稳定，而两个商品卡片之间误采会体现为位次变化。
    """

    if ancestor is None:
        return None
    parent = getattr(ancestor, "parent", None)
    if parent is None:
        return 0
    siblings = [
        child
        for child in parent.children
        if getattr(child, "name", None) == ancestor.name and _is_record_like(child)
    ]
    try:
        return siblings.index(ancestor)
    except ValueError:
        return 0


def _heading_text(ancestor: Any) -> str:
    if ancestor is None:
        return ""
    for level in range(1, 7):
        heading = ancestor.find(f"h{level}")
        if heading is not None:
            return " ".join(heading.get_text(" ", strip=True).split())
    return ""


def _object_signature(node: Any) -> dict[str, Any]:
    ancestor = _record_ancestor(node)
    heading = _heading_text(ancestor)
    return {
        "tag": getattr(ancestor, "name", None),
        "classes": sorted(_classes(ancestor)) if ancestor is not None else [],
        "heading": heading,
        "record_index": _record_index(ancestor),
        "node": _node_label(node),
    }


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in sorted(value.items())}
    return str(value)


def _fingerprint_dict(outcome: FieldOutcome) -> dict[str, Any]:
    return {
        "normalized_value": _json_value(outcome.extraction.normalized_value),
        "normalized_values": _json_value(outcome.extraction.normalized_values),
        "raw_values": _json_value(outcome.extraction.raw_values),
        "match_count": outcome.extraction.match_count,
        "conversion_status": outcome.extraction.conversion_status,
        "used_selector": outcome.extraction.used_selector,
        "flags": sorted(set(outcome.flags)),
        "score": outcome.score,
        "status": outcome.status,
    }


# ---------------------------------------------------------------------------
# 快照与候选
# ---------------------------------------------------------------------------


def _matching_snapshots(bundle: InputBundle, recipe: Recipe) -> list[LoadedSnapshot]:
    matching = [
        item
        for item in bundle.snapshots
        if item.meta.site == recipe.site and item.meta.page_type == recipe.page_type
    ]
    return sorted(matching, key=lambda item: (item.meta.version, item.meta.path))


def _latest_snapshot(snapshots: Sequence[LoadedSnapshot]) -> LoadedSnapshot:
    return max(snapshots, key=lambda item: (item.meta.version, item.meta.path))


def _required_snapshots(
    snapshots: Sequence[LoadedSnapshot], scope: str
) -> list[tuple[str, str]]:
    if scope == "current":
        latest = _latest_snapshot(snapshots)
        return [(latest.meta.version, latest.meta.path)]
    return [(item.meta.version, item.meta.path) for item in snapshots]


def _field_spec(recipe: Recipe, field_name: str) -> FieldSpec:
    for spec in recipe.fields:
        if spec.name == field_name:
            return spec
    raise InputError("FIELD_NOT_FOUND", f"配方 {recipe.name} 中没有字段: {field_name}")


def _collect_candidates(
    recipe: Recipe,
    spec: FieldSpec,
    snapshots: Sequence[LoadedSnapshot],
    scope: str,
) -> list[dict[str, Any]]:
    relevant = snapshots if scope == "replay" else [_latest_snapshot(snapshots)]
    merged: dict[str, dict[str, Any]] = {}
    for loaded in relevant:
        selection = match_nodes(loaded.soup, spec)
        for entry in enumerate_repair_candidates(loaded.soup, spec, selection, limit=12):
            selector = str(entry.get("selector", "")).strip()
            if not selector or selector in spec.selectors:
                continue
            current = merged.get(selector)
            version = loaded.meta.version
            if current is None:
                record = dict(entry)
                record["snapshots"] = [version]
                merged[selector] = record
                continue
            current["snapshots"] = sorted(set(current["snapshots"]) | {version})
            if int(entry.get("score", 0)) > int(current.get("score", 0)):
                snapshots_seen = current["snapshots"]
                record = dict(entry)
                record["snapshots"] = snapshots_seen
                merged[selector] = record
    ordered = sorted(
        merged.values(),
        key=lambda item: (-int(item.get("score", 0)), item["selector"]),
    )
    return ordered[:MAX_CANDIDATES_PER_FIELD]


def _actions_for_field(
    field_name: str,
    spec: FieldSpec,
    candidates: Sequence[dict[str, Any]],
) -> list[FieldAction]:
    actions: list[FieldAction] = []
    for candidate in candidates:
        selector = str(candidate["selector"])
        replace_selectors = (selector,) + tuple(
            item for item in spec.selectors[1:] if item != selector
        )
        actions.append(
            FieldAction(
                field_name=field_name,
                action="replace",
                selectors=tuple(dict.fromkeys(replace_selectors)),
                added_selectors=(selector,),
                candidate_selector=selector,
                source=str(candidate.get("source", "")),
                suggestion_score=int(candidate.get("score", 0)),
                suggestion_confidence=str(candidate.get("confidence", "low")),
                suggestion_reason=str(candidate.get("reason", "")),
            )
        )
        actions.append(
            FieldAction(
                field_name=field_name,
                action="append",
                selectors=tuple(dict.fromkeys(tuple(spec.selectors) + (selector,))),
                added_selectors=(selector,),
                candidate_selector=selector,
                source=str(candidate.get("source", "")),
                suggestion_score=int(candidate.get("score", 0)),
                suggestion_confidence=str(candidate.get("confidence", "low")),
                suggestion_reason=str(candidate.get("reason", "")),
            )
        )
    actions.append(
        FieldAction(
            field_name=field_name,
            action="abandon",
            selectors=tuple(spec.selectors),
            added_selectors=(),
            candidate_selector=None,
            source="abandon",
            suggestion_score=0,
            suggestion_confidence="low",
            suggestion_reason="放弃修复，保留原选择器；用于证明失败仍存在。",
        )
    )
    return sorted(actions, key=lambda item: item.rank)


# ---------------------------------------------------------------------------
# 真实重跑
# ---------------------------------------------------------------------------


def _load_baselines(
    config: AppConfig, bundle: InputBundle
) -> dict[str, Baseline]:
    cache: dict[str, Baseline] = {}
    for recipe in bundle.recipes:
        if recipe.baseline:
            cache[recipe.name] = load_baseline(recipe.baseline, config.root)
    return cache


def _rehearse(
    config: AppConfig,
    bundle: InputBundle,
    baselines: dict[str, Baseline],
    patched_specs: dict[tuple[str, str], FieldSpec],
) -> Rehearsal:
    rules = dict(config.rules)
    outcomes: list[FieldOutcome] = []
    findings: list[Finding] = []
    diffs: list[BaselineDiff] = []
    patched_recipes: dict[str, Recipe] = {}
    for recipe in bundle.recipes:
        specs = tuple(
            patched_specs.get((recipe.name, spec.name), spec)
            for spec in recipe.fields
        )
        patched_recipe = replace(recipe, fields=specs)
        patched_recipes[recipe.name] = patched_recipe
        matching = _matching_snapshots(bundle, recipe)
        recipe_outcomes: list[FieldOutcome] = []
        for loaded in matching:
            snapshot_outcomes, snapshot_findings = diagnose_recipe_snapshot(
                recipe=patched_recipe,
                snapshot=loaded.meta,
                soup=loaded.soup,
                specs=specs,
                rules=rules,
            )
            recipe_outcomes.extend(snapshot_outcomes)
            findings.extend(snapshot_findings)
        findings.extend(apply_version_drift(patched_recipe, recipe_outcomes, rules))
        outcomes.extend(recipe_outcomes)
        baseline = baselines.get(recipe.name)
        if baseline is not None:
            diffs.extend(compare_baseline(patched_recipe, baseline, recipe_outcomes, rules))
    outcomes.sort(key=lambda item: item.sort_key)
    findings.sort(
        key=lambda item: (
            item.recipe_name, item.site, item.page_type, item.version,
            item.snapshot_path, item.field_name or "", item.code, item.severity,
            item.message,
        )
    )
    diffs.sort(
        key=lambda item: (
            item.recipe_name, item.site, item.page_type, item.field_name,
            item.change_type, item.snapshot_path,
        )
    )
    status, summary = summarize(
        outcomes,
        findings,
        diffs,
        recipe_count=len(bundle.recipes),
        snapshot_count=len(bundle.snapshots),
    )
    summary["input_warning_count"] = len(bundle.warnings)
    return Rehearsal(
        recipes=patched_recipes,
        outcomes=outcomes,
        findings=findings,
        diffs=diffs,
        status=status,
        summary=summary,
        outcome_map={_outcome_key(item): item for item in outcomes},
    )


# ---------------------------------------------------------------------------
# 计划评估
# ---------------------------------------------------------------------------


def _base_scope_clean(
    base_outcomes: Sequence[FieldOutcome],
    *,
    recipe_name: str,
    field_name: str,
    required: Sequence[tuple[str, str]],
) -> bool:
    ok, _ = field_scope_clean(
        base_outcomes,
        recipe_name=recipe_name,
        field_name=field_name,
        required_snapshots=required,
    )
    return ok


def _evaluate_plan(
    *,
    config: AppConfig,
    bundle: InputBundle,
    baselines: dict[str, Baseline],
    base_outcomes: Sequence[FieldOutcome],
    base_diffs: Sequence[BaselineDiff],
    recipe: Recipe,
    snapshots: Sequence[LoadedSnapshot],
    scope: str,
    actions: dict[str, FieldAction],
    target_fields: Sequence[str],
) -> dict[str, Any]:
    patched_specs: dict[tuple[str, str], FieldSpec] = {}
    for field_name, action in actions.items():
        original = _field_spec(recipe, field_name)
        patched_specs[(recipe.name, field_name)] = replace(
            original, selectors=tuple(action.selectors)
        )
    rehearsal = _rehearse(config, bundle, baselines, patched_specs)
    required = _required_snapshots(snapshots, scope)
    conflicts: list[dict[str, Any]] = []
    target_results: dict[str, Any] = {}
    affected: list[dict[str, Any]] = []
    baseline = baselines.get(recipe.name)

    # 1) 目标字段在授权范围要求快照上的真实抽取结果。
    for field_name in target_fields:
        action = actions[field_name]
        clean, details = field_scope_clean(
            rehearsal.outcomes,
            recipe_name=recipe.name,
            field_name=field_name,
            required_snapshots=required,
        )
        target_results[field_name] = {
            "required_scope": scope,
            "required_snapshots": [{"version": v, "snapshot_path": p} for v, p in required],
            "clean": clean,
            "snapshots": details,
        }
        if not clean:
            for detail in details:
                if detail["ok"]:
                    continue
                outcome = rehearsal.outcome_map.get(
                    (recipe.name, detail["version"], detail["snapshot_path"], field_name)
                )
                conflicts.append(
                    _conflict(
                        "scope_not_met",
                        field_name=field_name,
                        version=detail["version"],
                        snapshot_path=detail["snapshot_path"],
                        selector=action.candidate_selector
                        or (outcome.extraction.used_selector if outcome else None),
                        message=(
                            f"{scope} 范围要求 {field_name} 在 {detail['version']} 可交付，"
                            f"实际为 {detail['conversion_status']} / {', '.join(detail['flags']) or '-'}"
                        ),
                        expected="ok",
                        actual=detail["conversion_status"],
                        evidence={
                            "flags": detail["flags"],
                            "action": action.to_dict(),
                        },
                    )
                )

    # 2) 基线与类型冲突（只看真实抽取结果，不看候选评分）。
    latest = _latest_snapshot(snapshots)
    for field_name in target_fields:
        action = actions[field_name]
        outcome = rehearsal.outcome_map.get(
            (recipe.name, latest.meta.version, latest.meta.path, field_name)
        )
        if outcome is None or baseline is None:
            continue
        expected = baseline.fields.get(field_name)
        if expected is None:
            continue
        if expected["normalized_value"] != outcome.extraction.normalized_value:
            conflicts.append(
                _conflict(
                    "baseline_value_conflict",
                    field_name=field_name,
                    version=latest.meta.version,
                    snapshot_path=latest.meta.path,
                    selector=action.candidate_selector
                    or outcome.extraction.used_selector,
                    message=(
                        f"候选使 {field_name} 的最新值偏离基线："
                        f"{_json_value(outcome.extraction.normalized_value)!r} != "
                        f"{_json_value(expected['normalized_value'])!r}"
                    ),
                    expected=_json_value(expected["normalized_value"]),
                    actual=_json_value(outcome.extraction.normalized_value),
                    evidence={"action": action.to_dict()},
                )
            )
        if expected["conversion_status"] != outcome.extraction.conversion_status:
            conflicts.append(
                _conflict(
                    "baseline_status_conflict",
                    field_name=field_name,
                    version=latest.meta.version,
                    snapshot_path=latest.meta.path,
                    selector=action.candidate_selector
                    or outcome.extraction.used_selector,
                    message=(
                        f"候选使 {field_name} 的转换状态偏离基线："
                        f"{outcome.extraction.conversion_status}"
                    ),
                    expected=expected["conversion_status"],
                    actual=outcome.extraction.conversion_status,
                    evidence={"action": action.to_dict()},
                )
            )
        spec = _field_spec(recipe, field_name)
        if (
            not spec.multi
            and expected["match_count"] != outcome.extraction.match_count
        ):
            conflicts.append(
                _conflict(
                    "baseline_match_count_conflict",
                    field_name=field_name,
                    version=latest.meta.version,
                    snapshot_path=latest.meta.path,
                    selector=action.candidate_selector
                    or outcome.extraction.used_selector,
                    message=(
                        f"候选使 {field_name} 的匹配数量偏离基线："
                        f"{outcome.extraction.match_count} != {expected['match_count']}"
                    ),
                    expected=expected["match_count"],
                    actual=outcome.extraction.match_count,
                    evidence={"action": action.to_dict()},
                )
            )

    # 3) 跨字段节点误采：候选节点不得与其他字段（含其他目标字段）的节点重叠。
    for loaded in snapshots:
        version, path = loaded.meta.version, loaded.meta.path
        for field_name in target_fields:
            action = actions[field_name]
            original_spec = _field_spec(recipe, field_name)
            original_selection = match_nodes(loaded.soup, original_spec)
            spec = patched_specs[(recipe.name, field_name)]
            selection = match_nodes(loaded.soup, spec)
            if not selection.nodes:
                continue
            for other_spec in rehearsal.recipes[recipe.name].fields:
                if other_spec.name == field_name:
                    continue
                other_selection = match_nodes(loaded.soup, other_spec)
                for node in selection.nodes:
                    for other_node in other_selection.nodes:
                        relation = _node_related(node, other_node)
                        if relation is None:
                            continue
                        # 原配方本身就存在的重叠不算候选引入的新问题。
                        preexisting = any(
                            _node_related(old_node, other_node) is not None
                            for old_node in original_selection.nodes
                        )
                        if preexisting and _node_related(node, other_node) == relation:
                            continue
                        conflicts.append(
                            _conflict(
                                "cross_field_node_overlap",
                                field_name=field_name,
                                version=version,
                                snapshot_path=path,
                                selector=action.candidate_selector or selection.used_selector,
                                message=(
                                    f"{field_name} 的候选与其他字段 {other_spec.name} 的节点重叠"
                                    f"（{relation}），可能误采其他业务字段"
                                ),
                                expected=other_spec.name,
                                actual=field_name,
                                evidence={
                                    "other_field": other_spec.name,
                                    "other_selector": other_selection.used_selector,
                                    "node": _node_label(node),
                                    "other_node": _node_label(other_node),
                                    "relation": relation,
                                },
                            )
                        )

    # 4) 业务对象一致性：候选节点所在的记录/卡片必须与原字段成功时一致。
    for field_name in target_fields:
        action = actions[field_name]
        original_spec = _field_spec(recipe, field_name)
        references = []
        for loaded in snapshots:
            old = next(
                (
                    item
                    for item in base_outcomes
                    if item.recipe_name == recipe.name
                    and item.field_name == field_name
                    and item.version == loaded.meta.version
                    and item.snapshot_path == loaded.meta.path
                ),
                None,
            )
            if old is None or not outcome_is_clean(old):
                continue
            old_selection = match_nodes(loaded.soup, original_spec)
            if old_selection.nodes:
                references.append(_object_signature(old_selection.nodes[0]))
        if not references:
            continue
        for loaded in snapshots:
            outcome = rehearsal.outcome_map.get(
                (recipe.name, loaded.meta.version, loaded.meta.path, field_name)
            )
            if outcome is None or not outcome_is_clean(outcome):
                continue
            spec = patched_specs[(recipe.name, field_name)]
            selection = match_nodes(loaded.soup, spec)
            if not selection.nodes:
                continue
            candidate = _object_signature(selection.nodes[0])
            same_object = any(
                candidate["tag"] == item["tag"]
                and candidate["record_index"] == item["record_index"]
                for item in references
            )
            if same_object:
                continue
            conflicts.append(
                _conflict(
                    "business_object_mismatch",
                    field_name=field_name,
                    version=loaded.meta.version,
                    snapshot_path=loaded.meta.path,
                    selector=action.candidate_selector or selection.used_selector,
                    message=(
                        f"{field_name} 的候选落在不同的业务对象："
                        f"候选记录位次 {candidate['record_index']} / 原对象 "
                        f"{sorted({item['record_index'] for item in references})}"
                    ),
                    expected=[
                        {
                            "tag": item["tag"],
                            "classes": item["classes"],
                            "record_index": item["record_index"],
                        }
                        for item in references
                    ],
                    actual={
                        "tag": candidate["tag"],
                        "classes": candidate["classes"],
                        "record_index": candidate["record_index"],
                    },
                    evidence={
                        "candidate_object": candidate,
                        "reference_objects": references,
                    },
                )
            )

    # 5) 跨版本漂移冲突：候选让原本稳定的目标字段产生新的值漂移。
    for field_name in target_fields:
        action = actions[field_name]
        new_items = sorted(
            (
                item
                for item in rehearsal.outcomes
                if item.recipe_name == recipe.name and item.field_name == field_name
            ),
            key=lambda item: (item.version, item.snapshot_path),
        )
        old_items = sorted(
            (
                item
                for item in base_outcomes
                if item.recipe_name == recipe.name and item.field_name == field_name
            ),
            key=lambda item: (item.version, item.snapshot_path),
        )
        old_drift = any("version_drift" in item.flags for item in old_items)
        new_drift = any("version_drift" in item.flags for item in new_items)
        values = [item.extraction.normalized_value for item in new_items if item.extraction.has_value]
        distinct = {canonical_digest(_json_value(value)) for value in values}
        if new_drift and not old_drift and len(distinct) > 1:
            conflicts.append(
                _conflict(
                    "version_drift_conflict",
                    field_name=field_name,
                    version=new_items[-1].version if new_items else None,
                    snapshot_path=new_items[-1].snapshot_path if new_items else None,
                    selector=action.candidate_selector,
                    message=f"候选使 {field_name} 在归档版本之间产生新的值漂移",
                    expected="稳定值",
                    actual=[_json_value(item.extraction.normalized_value) for item in new_items],
                    evidence={"versions": [item.version for item in new_items]},
                )
            )

    # 6) 未列为目标的字段必须保持既有结果（值/匹配/状态/标记/评分/基线差异）。
    target_keys = {(recipe.name, name) for name in target_fields}
    for old in base_outcomes:
        key = _outcome_key(old)
        if (old.recipe_name, old.field_name) in target_keys:
            continue
        new = rehearsal.outcome_map.get(key)
        if new is None or not outcomes_equal(old, new):
            conflicts.append(
                _conflict(
                    "untargeted_field_changed",
                    field_name=old.field_name,
                    version=old.version,
                    snapshot_path=old.snapshot_path,
                    selector=old.extraction.used_selector,
                    message=f"方案改变了未列为目标的字段 {old.field_name} 的既有结果",
                    expected=_fingerprint_dict(old),
                    actual=_fingerprint_dict(new) if new is not None else None,
                )
            )
    base_projection = _diff_projection(base_diffs)
    new_projection = _diff_projection(rehearsal.diffs)
    for key in sorted(set(base_projection) | set(new_projection)):
        if key in target_keys:
            continue
        if new_projection.get(key, []) != base_projection.get(key, []):
            conflicts.append(
                _conflict(
                    "untargeted_baseline_changed",
                    field_name=key[1],
                    message=f"方案改变了未列为目标的字段 {key[1]} 的基线差异结论",
                    expected=base_projection.get(key, []),
                    actual=new_projection.get(key, []),
                    evidence={"recipe_name": key[0]},
                )
            )

    # 7) current 范围下对归档版本的影响必须显式列出，不能悄悄回归。
    for field_name in target_fields:
        action = actions[field_name]
        for loaded in snapshots:
            key = (recipe.name, loaded.meta.version, loaded.meta.path, field_name)
            old = next((item for item in base_outcomes if _outcome_key(item) == key), None)
            new = rehearsal.outcome_map.get(key)
            if old is None or new is None:
                continue
            old_clean = outcome_is_clean(old)
            new_clean = outcome_is_clean(new)
            if old_clean and not new_clean:
                affected.append(
                    {
                        "field_name": field_name,
                        "version": loaded.meta.version,
                        "snapshot_path": loaded.meta.path,
                        "selector": action.candidate_selector or new.extraction.used_selector,
                        "before_conversion_status": old.extraction.conversion_status,
                        "after_conversion_status": new.extraction.conversion_status,
                        "after_flags": sorted(set(new.flags)),
                        "scope_required": (loaded.meta.version, loaded.meta.path) in required,
                    }
                )

    # 8) 计划级状态由同一套 regression 规则计算。
    conflicts = _dedupe_conflicts(conflicts)
    changed_fields = sorted(
        actions, key=lambda name: _field_index(recipe, name)
    )
    added = sorted(
        {
            selector
            for action in actions.values()
            for selector in action.added_selectors
        }
    )
    scope_met = all(
        target_results[name]["clean"] for name in target_fields
    )
    feasible = scope_met and not conflicts
    rank_key = (
        0 if feasible else 1,
        len(changed_fields),
        len(added),
        tuple(
            (ACTION_ORDER.get(actions[name].action, 9), actions[name].candidate_selector or "")
            for name in changed_fields
        ),
        tuple(actions[name].selectors for name in changed_fields),
    )
    return {
        "actions": [actions[name].to_dict() for name in changed_fields],
        "changed_fields": changed_fields,
        "changed_field_count": len(changed_fields),
        "added_selectors": added,
        "added_selector_count": len(added),
        "scope_met": scope_met,
        "feasible": feasible,
        "target_results": target_results,
        "affected_snapshots": sorted(
            affected,
            key=lambda item: (
                _field_index(recipe, item["field_name"]),
                item["version"],
                item["snapshot_path"],
            ),
        ),
        "conflicts": conflicts,
        "run_status": rehearsal.status,
        "summary": rehearsal.summary,
        "_rank_key": rank_key,
    }


def _field_index(recipe: Recipe, field_name: str) -> int:
    for index, spec in enumerate(recipe.fields):
        if spec.name == field_name:
            return index
    return len(recipe.fields)


def _diff_projection(diffs: Sequence[BaselineDiff]) -> dict[tuple[str, str], list[tuple[Any, ...]]]:
    grouped: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
    for item in diffs:
        grouped.setdefault((item.recipe_name, item.field_name), []).append(
            (
                item.change_type,
                item.severity,
                _json_value(item.old_value),
                _json_value(item.new_value),
            )
        )
    for key in grouped:
        grouped[key].sort(key=lambda item: tuple(str(part) for part in item))
    return grouped


# ---------------------------------------------------------------------------
# 计划编排
# ---------------------------------------------------------------------------


def _normalize_fields(fields: Sequence[str]) -> list[str]:
    output: list[str] = []
    for raw in fields:
        for part in str(raw).split(","):
            name = part.strip()
            if name and name not in output:
                output.append(name)
    if not output:
        raise InputError("FIELD_NOT_FOUND", "至少需要一个 --field")
    return output


def _plan_digest(plan: dict[str, Any]) -> str:
    semantic = {
        "schema_version": plan["schema_version"],
        "kind": plan["kind"],
        "scope": plan["scope"],
        "recipe": plan["recipe"],
        "requested_fields": plan["requested_fields"],
        "failing_fields": plan["failing_fields"],
        "field_actions": plan["field_actions"],
        "evaluations": [
            {
                "actions": item["actions"],
                "feasible": item["feasible"],
                "conflicts": item["conflicts"],
                "target_results": item["target_results"],
                "affected_snapshots": item["affected_snapshots"],
            }
            for item in plan["evaluations"]
        ],
        "recommended": plan["recommended"],
        "conflicts": plan["conflicts"],
        "replace_deliverable": plan["replace_deliverable"],
    }
    return canonical_digest(semantic)[:16]


def compute_repair_plan(
    config: AppConfig,
    bundle: InputBundle,
    base_result: Any,
    *,
    recipe_name: str,
    fields: Sequence[str],
    scope: str,
) -> dict[str, Any]:
    """在内存中计算修复预演，不写回配方/快照。"""

    if scope not in SCOPES:
        raise InputError("INVALID_SCOPE", f"未知授权范围: {scope}")
    requested = _normalize_fields(fields)
    recipe = next((item for item in bundle.recipes if item.name == recipe_name), None)
    if recipe is None:
        raise InputError("RECIPE_NOT_FOUND", f"配置中没有配方: {recipe_name}")
    for field_name in requested:
        _field_spec(recipe, field_name)
    snapshots = _matching_snapshots(bundle, recipe)
    if not snapshots:
        raise InputError(
            "NO_SNAPSHOTS_FOR_RECIPE",
            f"配方 {recipe.name} 没有匹配的快照: {recipe.site}/{recipe.page_type}",
        )
    required = _required_snapshots(snapshots, scope)
    base_outcomes = list(base_result.outcomes)
    failing = [
        name
        for name in requested
        if not _base_scope_clean(
            base_outcomes,
            recipe_name=recipe.name,
            field_name=name,
            required=required,
        )
    ]
    baselines = _load_baselines(config, bundle)
    candidates_by_field = {
        name: _collect_candidates(recipe, _field_spec(recipe, name), snapshots, scope)
        for name in failing
    }
    actions_by_field = {
        name: _actions_for_field(
            name, _field_spec(recipe, name), candidates_by_field[name]
        )
        for name in failing
    }

    # 单字段动作预演：既作为可审核的字段级摘要，也用于裁剪组合空间。
    base_diffs = list(base_result.diffs)
    field_actions: list[dict[str, Any]] = []
    usable: dict[str, list[FieldAction]] = {}
    for name in failing:
        previews: list[dict[str, Any]] = []
        for action in actions_by_field[name]:
            evaluation = _evaluate_plan(
                config=config,
                bundle=bundle,
                baselines=baselines,
                base_outcomes=base_outcomes,
                base_diffs=base_diffs,
                recipe=recipe,
                snapshots=snapshots,
                scope=scope,
                actions={name: action},
                target_fields=[name],
            )
            preview = action.to_dict()
            preview.update(
                {
                    "target_scope_met": evaluation["scope_met"],
                    "feasible_for_field": evaluation["feasible"],
                    "target_results": evaluation["target_results"],
                    "affected_snapshots": evaluation["affected_snapshots"],
                    "conflicts": evaluation["conflicts"],
                    "run_status": evaluation["run_status"],
                }
            )
            previews.append(preview)
        previews.sort(
            key=lambda item: (
                ACTION_ORDER.get(item["action"], 9),
                -int(item["suggestion"]["score"]),
                item["candidate_selector"] or "",
                item["selectors"],
            )
        )
        field_actions.extend(previews)
        conflict_free = [
            item
            for item in previews
            if item["action"] != "abandon"
            and item["target_scope_met"]
            and item["feasible_for_field"]
        ]
        scope_met = [
            item
            for item in previews
            if item["action"] != "abandon" and item["target_scope_met"]
        ]
        importable = conflict_free or scope_met
        if not importable:
            importable = [item for item in previews if item["action"] != "abandon"][:3]
        # 组合优先使用单字段即可交付且无冲突的动作；顺序固定为动作/候选/选择器。
        importable.sort(
            key=lambda item: (
                ACTION_ORDER.get(item["action"], 9),
                -int(item["suggestion"]["score"]),
                item["candidate_selector"] or "",
                item["selectors"],
            )
        )
        usable[name] = [
            next(
                action
                for action in actions_by_field[name]
                if action.action == item["action"]
                and action.selectors == tuple(item["selectors"])
            )
            for item in importable[:MAX_ACTIONS_PER_FIELD]
        ]

    # 全计划组合预演：以最少字段/最少候选/稳定顺序择优。
    combinations: list[tuple[FieldAction, ...]] = []
    ordered_fields = sorted(
        (name for name in requested if name in failing),
        key=lambda name: _field_index(recipe, name),
    )
    if ordered_fields:
        pools = [usable.get(name) or actions_by_field[name][:1] for name in ordered_fields]
        for combo in product(*pools):
            combinations.append(combo)
            if len(combinations) >= MAX_PLANS:
                break
    else:
        combinations.append(())

    evaluations: list[dict[str, Any]] = []
    for combo in combinations:
        actions = {
            name: action for name, action in zip(ordered_fields, combo)
        }
        evaluations.append(
            _evaluate_plan(
                config=config,
                bundle=bundle,
                baselines=baselines,
                base_outcomes=base_outcomes,
                base_diffs=base_diffs,
                recipe=recipe,
                snapshots=snapshots,
                scope=scope,
                actions=actions,
                target_fields=ordered_fields,
            )
        )
    evaluations.sort(key=lambda item: item["_rank_key"])
    for item in evaluations:
        item.pop("_rank_key", None)

    feasible = [item for item in evaluations if item["feasible"]]
    recommended = feasible[0] if feasible else None
    if recommended is not None:
        conflicts = list(recommended["conflicts"])
    else:
        evidence = min(
            evaluations,
            key=lambda item: (
                len(item["conflicts"]),
                item["changed_field_count"],
                item["added_selector_count"],
            ),
        )
        conflicts = _dedupe_conflicts(
            [conflict for item in evaluations for conflict in item["conflicts"]]
            + [
                conflict
                for preview in field_actions
                for conflict in preview["conflicts"]
            ]
        )
        if not conflicts:
            conflicts = evidence["conflicts"]
        if not failing:
            conflicts = []
    conflicts = _dedupe_conflicts(conflicts)

    replace_previews = [
        item
        for item in field_actions
        if item["action"] == "replace" and item["field_name"] in failing
    ]
    replace_deliverable = any(item["feasible_for_field"] for item in replace_previews)

    plan: dict[str, Any] = {
        "schema_version": PLAN_SCHEMA_VERSION,
        "kind": "selregress.selector_repair_plan",
        "plan_id": "",
        "command": "plan-selector-repair",
        "generated_at": base_result.generated_at,
        "scope": scope,
        "scope_semantics": {
            "current": "只承诺最新快照的抽取；归档版本若受影响会在 affected_snapshots 中列出，但不作为交付门槛。",
            "replay": "承诺全部归档版本仍可回放；任一归档版本无法命中的方案不可交付。",
            "applied": (
                "current：仅最新版本必须可交付；归档影响显式披露。"
                if scope == "current"
                else "replay：全部归档版本都必须可交付。"
            ),
            "required_snapshots": [{"version": v, "snapshot_path": p} for v, p in required],
        },
        "base_run": {
            "run_id": base_result.run_id,
            "status": base_result.status,
            "summary": base_result.summary,
        },
        "recipe": {
            "name": recipe.name,
            "path": recipe.path,
            "sha256": recipe.sha256,
            "site": recipe.site,
            "page_type": recipe.page_type,
        },
        "requested_fields": list(requested),
        "failing_fields": list(failing),
        "field_actions": field_actions,
        "replace_deliverable": replace_deliverable,
        "feasible": recommended is not None,
        "evaluations": evaluations,
        "recommended": recommended,
        "conflicts": conflicts,
        "read_only": True,
        "mutates_inputs": False,
        "notes": [
            "候选建议的 score/confidence 仅作线索，交付性由真实重跑结果决定。",
            "未列为目标的字段结果必须与旧运行逐项一致，否则方案不可交付。",
            "本命令不写回 YAML，不修改快照，不部署生产选择器。",
        ],
    }
    plan["plan_id"] = _plan_digest(plan)
    return plan


def plan_selector_repair(
    config: AppConfig,
    *,
    recipe_name: str,
    fields: Sequence[str],
    scope: str,
    fixed_time: str | None = None,
    ledger_path: Any = None,
    runs_dir: Any = None,
) -> dict[str, Any]:
    """CLI/测试入口：先记录旧运行，再做只读预演并写入台账与计划报告。"""

    execution = execute(
        config,
        fixed_time=fixed_time,
        ledger_path=ledger_path,
        runs_dir=runs_dir,
        fail_on_warn=False,
    )
    bundle = execution.bundle or load_bundle(config)
    plan = compute_repair_plan(
        config,
        bundle,
        execution.result,
        recipe_name=recipe_name,
        fields=fields,
        scope=scope,
    )
    artifacts = write_repair_plan_reports(
        plan,
        base_report_dir=execution.result.report_dir,
        root=config.root,
    )
    plan["artifacts"] = artifacts
    ledger = Ledger(ledger_path or config.ledger_path, config.root)
    ledger.record_repair_plan(plan)
    return plan
