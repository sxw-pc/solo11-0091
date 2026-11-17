"""JSON / Markdown / 自包含 HTML 报告生成。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .models import FieldOutcome, Finding, RunResult
from .util import (
    PROJECT_ROOT, atomic_write_text, relative_posix, resolve_under_root, severity_rank,
    sha256_text, stable_json_text, truncate,
)

REPORT_SCHEMA_VERSION = 1


def _finding_sort(item: Finding) -> tuple[Any, ...]:
    return (
        severity_rank(item.severity), item.code, item.recipe_name, item.site,
        item.page_type, item.version, item.snapshot_path, item.field_name or "", item.message,
    )


def _outcome_sort(item: FieldOutcome) -> tuple[Any, ...]:
    return item.sort_key


def sorted_findings(findings: list[Finding]) -> list[Finding]:
    return sorted(findings, key=_finding_sort)


def build_report(run: RunResult) -> dict[str, Any]:
    outcomes = sorted(run.outcomes, key=_outcome_sort)
    findings = sorted_findings(run.findings)
    diffs = sorted(run.diffs, key=lambda item: (
        item.recipe_name, item.site, item.page_type, item.field_name,
        item.change_type, item.snapshot_path,
    ))
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "report_kind": "selregress.run",
        "run": {
            "run_id": run.run_id,
            "status": run.status,
            "generated_at": run.generated_at,
            "fixed_time": run.fixed_time,
            "config_path": run.config_path,
            "fail_on_warn": run.fail_on_warn,
            "report_dir": run.report_dir,
        },
        "summary": dict(sorted(run.summary.items())),
        "recipes": sorted(run.recipes, key=lambda item: (item["name"], item["path"])),
        "snapshots": sorted(run.snapshots, key=lambda item: (
            item["site"], item["page_type"], item["version"], item["path"])),
        "fields": [item.to_dict() for item in outcomes],
        "findings": [item.to_dict() for item in findings],
        "baseline_diffs": [item.to_dict() for item in diffs],
    }


def _md(value: Any) -> str:
    text = "" if value is None else str(value)
    return truncate(text.replace("|", "\\|").replace("\n", " "), 160)


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    run = report["run"]
    lines = [
        "# 择约回归台运行报告",
        "",
        f"- 运行 ID：`{run['run_id']}`",
        f"- 结论：**{run['status']}**",
        f"- 生成时间：`{run['generated_at']}`",
        f"- 固定时间：`{str(run['fixed_time']).lower()}`",
        f"- 配置：`{run['config_path']}`",
        "",
        "## 总览",
        "",
        "| 指标 | 数值 |",
        "| --- | ---: |",
        f"| 配方数 | {summary['recipe_count']} |",
        f"| 快照数 | {summary['snapshot_count']} |",
        f"| 字段结果数 | {summary['field_result_count']} |",
        f"| 失败 / 警告 / 信息发现 | {summary['fail_finding_count']} / {summary['warn_finding_count']} / {summary['info_finding_count']} |",
        f"| 基线差异数 | {summary['baseline_diff_count']} |",
        f"| 平均稳定性 | {summary['average_stability']} |",
        "",
        "## 字段矩阵",
        "",
        "| 配方 | 版本 | 字段 | 状态 | 命中 | 转换 | 稳定性 | 标记 |",
        "| --- | --- | --- | --- | ---: | --- | ---: | --- |",
    ]
    for field in report["fields"]:
        lines.append(
            f"| {_md(field['recipe_name'])} | {_md(field['version'])} | {_md(field['field_name'])} "
            f"| {field['status']} | {field['match_count']} | {_md(field['conversion_status'])} "
            f"| {field['score']} | {_md(', '.join(field['flags']) or '-')} |"
        )
    failed = [item for item in report["fields"] if item["status"] == "fail"]
    lines += ["", "## 失败字段", ""]
    if failed:
        for item in failed:
            lines.append(
                f"- `{item['recipe_name']}.{item['field_name']}` @ {item['version']}："
                f"{_md(item['error'] or ', '.join(item['flags']))}"
            )
    else:
        lines.append("无。")
    lines += ["", "## 发现项", ""]
    if report["findings"]:
        for item in report["findings"]:
            field = f".{item['field_name']}" if item["field_name"] else ""
            lines.append(
                f"- **{item['severity']}** `{item['code']}` "
                f"`{item['recipe_name']}{field}` @ {item['version']}：{_md(item['message'])}"
            )
    else:
        lines.append("无。")
    drift = [item for item in report["findings"] if item["code"] == "version_drift"]
    lines += ["", "## 跨版本漂移", ""]
    if drift:
        for item in drift:
            pair = item["evidence"].get("version_pair", [])
            lines.append(
                f"- `{item['recipe_name']}.{item['field_name']}`："
                f"{' → '.join(str(value) for value in pair)}，"
                f"字段路径 `{item['evidence'].get('field_path', '')}`"
            )
    else:
        lines.append("无。")
    lines += ["", "## 候选选择器建议", ""]
    suggestions = [
        (item, candidate) for item in report["findings"] for candidate in item.get("suggestions", [])
    ]
    if suggestions:
        for item, candidate in suggestions:
            lines.append(
                f"- `{candidate['selector']}`（{candidate['confidence']}，{candidate['score']} 分）"
                f"用于 `{item['recipe_name']}.{item['field_name']}`：{_md(candidate['reason'])} "
                f"风险：{_md(candidate['risk'])}"
            )
    else:
        lines.append("无。")
    lines += ["", "## 基线差异", ""]
    if report["baseline_diffs"]:
        lines += [
            "| 字段 | 类型 | 级别 | 旧值 | 新值 |",
            "| --- | --- | --- | --- | --- |",
        ]
        for item in report["baseline_diffs"]:
            lines.append(
                f"| {_md(item['recipe_name'] + '.' + item['field_name'])} | {item['change_type']} "
                f"| {item['severity']} | {_md(item['old_value'])} | {_md(item['new_value'])} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "> 本报告仅用于分析本人拥有或已获授权的本地快照材料。候选建议为只读线索，不会写回配方。",
        "",
    ]
    return "\n".join(lines)


def render_html(report: dict[str, Any], template_dir: Path) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=select_autoescape(["html", "xml"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    return environment.get_template("report.html").render(report=report)


def write_reports(run: RunResult, runs_dir: Path, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    report_dir = resolve_under_root(runs_dir / run.run_id, root, "report_dir")
    run.report_dir = relative_posix(report_dir, root)
    report_data = build_report(run)
    json_text = stable_json_text(report_data)
    markdown = render_markdown(report_data)
    html = render_html(report_data, Path(__file__).resolve().parent / "templates")
    atomic_write_text(report_dir / "report.json", json_text)
    atomic_write_text(report_dir / "report.md", markdown)
    atomic_write_text(report_dir / "report.html", html)
    hashes = {
        "json": sha256_text(json_text),
        "md": sha256_text(markdown),
        "html": sha256_text(html),
    }
    run.report_hashes = hashes
    return {
        "dir": run.report_dir,
        "files": {
            "json": f"{run.report_dir}/report.json",
            "markdown": f"{run.report_dir}/report.md",
            "html": f"{run.report_dir}/report.html",
        },
        "hashes": hashes,
    }


# ---------------------------------------------------------------------------
# 选择器修复预演报告
# ---------------------------------------------------------------------------


def repair_plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """CLI 与复核使用的计划摘要。候选评分只作为线索展示，不参与可行判定。"""

    recommended = plan.get("recommended")
    return {
        "plan_id": plan.get("plan_id", ""),
        "kind": plan.get("kind", "selregress.selector_repair_plan"),
        "scope": plan.get("scope"),
        "scope_semantics": plan.get("scope_semantics", {}),
        "recipe": plan.get("recipe", {}),
        "requested_fields": list(plan.get("requested_fields", [])),
        "failing_fields": list(plan.get("failing_fields", [])),
        "feasible": bool(plan.get("feasible", recommended is not None)),
        "replace_deliverable": bool(plan.get("replace_deliverable", False)),
        "base_run_id": (plan.get("base_run") or {}).get("run_id", ""),
        "recommended": (
            {
                "actions": recommended.get("actions", []),
                "changed_field_count": recommended.get("changed_field_count", 0),
                "added_selector_count": recommended.get("added_selector_count", 0),
                "run_status": recommended.get("run_status"),
                "summary": recommended.get("summary", {}),
                "affected_snapshots": recommended.get("affected_snapshots", []),
            }
            if recommended is not None
            else None
        ),
        "conflict_count": len(plan.get("conflicts", [])),
        "artifacts": plan.get("artifacts", {}),
        "read_only": True,
    }


def _md_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return truncate(text.replace("|", "\\|").replace("\n", " "), 160)


def render_repair_plan_markdown(plan: dict[str, Any]) -> str:
    summary = repair_plan_summary(plan)
    lines = [
        "# 择约回归台选择器修复预演计划",
        "",
        f"- 计划 ID：`{summary['plan_id']}`",
        f"- 授权范围：**{summary['scope']}**",
        f"- 配方：`{summary['recipe'].get('name', '')}`（`{summary['recipe'].get('path', '')}`）",
        f"- 旧运行：`{summary['base_run_id']}`",
        f"- 结论：**{'可行' if summary['feasible'] else '无可行方案'}**",
        f"- 替换主选择器可交付：**{'是' if summary['replace_deliverable'] else '否'}**",
        f"- 目标字段：{', '.join(summary['requested_fields']) or '-'}",
        f"- 失败字段：{', '.join(summary['failing_fields']) or '-'}",
        "",
        "## 授权范围语义",
        "",
        summary["scope_semantics"].get("applied", ""),
        "",
        "## 推荐方案",
        "",
    ]
    recommended = summary["recommended"]
    if recommended is None:
        lines.append("无可行方案。以下冲突证据用于人工复核。")
    else:
        lines += [
            "| 字段 | 动作 | 选择器 | 新增候选 | 建议分（仅线索） |",
            "| --- | --- | --- | --- | ---: |",
        ]
        for action in recommended["actions"]:
            lines.append(
                f"| {_md_cell(action['field_name'])} | {action['action']} "
                f"| {_md_cell(' → '.join(action['selectors']))} "
                f"| {_md_cell(', '.join(action['added_selectors']) or '-')} "
                f"| {action['suggestion']['score']} |"
            )
        lines += [
            "",
            f"- 改动字段数：{recommended['changed_field_count']}",
            f"- 新增候选数：{recommended['added_selector_count']}",
            f"- 预演运行状态：`{recommended['run_status']}`",
        ]
        affected = recommended.get("affected_snapshots", [])
        lines += ["", "### 受影响快照", ""]
        if affected:
            for item in affected:
                lines.append(
                    f"- `{item['field_name']}` @ {item['version']}："
                    f"{item['before_conversion_status']} → "
                    f"{item['after_conversion_status']} / "
                    f"{', '.join(item['after_flags']) or '-'}"
                    f"（选择器 `{item['selector']}`）"
                )
        else:
            lines.append("无。")
    lines += ["", "## 字段级动作预演", ""]
    if plan.get("field_actions"):
        lines += [
            "| 字段 | 动作 | 候选 | 建议分 | 范围达标 | 可交付 | 冲突数 |",
            "| --- | --- | --- | ---: | --- | --- | ---: |",
        ]
        for item in plan["field_actions"]:
            lines.append(
                f"| {_md_cell(item['field_name'])} | {item['action']} "
                f"| {_md_cell(item['candidate_selector'] or '-')} "
                f"| {item['suggestion']['score']} "
                f"| {'是' if item['target_scope_met'] else '否'} "
                f"| {'是' if item['feasible_for_field'] else '否'} "
                f"| {len(item['conflicts'])} |"
            )
    else:
        lines.append("目标字段在授权范围内均已达标，无需修改。")
    lines += ["", "## 冲突证据", ""]
    conflicts = plan.get("conflicts", [])
    if conflicts:
        lines += [
            "| 类型 | 字段 | 版本 | 快照 | 选择器 | 说明 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for item in conflicts:
            lines.append(
                f"| {item['kind']} | {_md_cell(item.get('field_name') or '-')} "
                f"| {_md_cell(item.get('version') or '-')} "
                f"| {_md_cell(item.get('snapshot_path') or '-')} "
                f"| {_md_cell(item.get('selector') or '-')} "
                f"| {_md_cell(item.get('message', ''))} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "## 计划级对照",
        "",
    ]
    evaluations = plan.get("evaluations", [])
    if evaluations:
        lines += [
            "| 字段动作 | 改动字段 | 新增候选 | 可行 | 冲突数 |",
            "| --- | ---: | ---: | --- | ---: |",
        ]
        for item in evaluations[:20]:
            actions = ", ".join(
                f"{action['field_name']}:{action['action']}"
                f"({action['candidate_selector'] or '-'})"
                for action in item["actions"]
            )
            lines.append(
                f"| {_md_cell(actions or '无改动')} | {item['changed_field_count']} "
                f"| {item['added_selector_count']} "
                f"| {'是' if item['feasible'] else '否'} "
                f"| {len(item['conflicts'])} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "> 本计划仅用于分析本人拥有或已获授权的本地快照材料。"
        "预演不会写回 YAML，不会修改快照，也不会部署生产选择器；"
        "候选建议的置信分不能作为修复成功的证据。",
        "",
    ]
    return "\n".join(lines)


def write_repair_plan_reports(
    plan: dict[str, Any], *, base_report_dir: str, root: Path = PROJECT_ROOT
) -> dict[str, Any]:
    """把计划写入旧运行报告目录旁的独立文件，并在台账外保留可审核副本。"""

    plan_dir = resolve_under_root(Path(base_report_dir), root, "plan_report_dir")
    plan_id = plan.get("plan_id", "plan")
    json_text = stable_json_text(plan)
    markdown = render_repair_plan_markdown(plan)
    json_path = plan_dir / f"selector-repair-{plan_id}.json"
    md_path = plan_dir / f"selector-repair-{plan_id}.md"
    atomic_write_text(json_path, json_text)
    atomic_write_text(md_path, markdown)
    return {
        "dir": relative_posix(plan_dir, root),
        "files": {
            "json": relative_posix(json_path, root),
            "markdown": relative_posix(md_path, root),
        },
        "hashes": {
            "json": sha256_text(json_text),
            "md": sha256_text(markdown),
        },
    }
