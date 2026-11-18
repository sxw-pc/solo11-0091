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


def render_asset_contract_markdown(evidence: dict[str, Any]) -> str:
    """渲染本地照片许可清点的人类可读裁决（完整证据仍以 JSON 为准）。"""
    decisions = evidence["license_decisions"]
    lines = [
        "# 本地授权照片许可清点",
        "",
        f"- 快照：`{evidence['snapshot']}`（SHA-256 `{evidence['snapshot_sha256'][:16]}…`）",
        f"- 每许可单元费用：{evidence['license_fee_per_unit']}",
        "- 用途：本地授权素材离线清点，不删图、不改配方、不联网。",
        "",
        "## 资产与主体证据",
        "",
        "| 资产 | SKU | 图片 | 尺寸 | 主体像素 | 主体占比 | SHA-256（前 12 位） |",
        "| --- | --- | --- | --- | ---: | ---: | --- |",
    ]
    for asset in evidence["assets"]:
        lines.append(
            f"| {asset['asset_id']} | {_md(asset['sku'])} | `{_md(asset['image_src'])}` "
            f"| {asset['width']}×{asset['height']} | {asset['subject_pixel_count']} "
            f"| {asset['subject_coverage_ratio']:.3f} | `{asset['sha256'][:12]}` |"
        )
    lines += ["", "## 按合同裁决（两份合同各自独立，互不替代）", ""]
    for contract in evidence["contracts"]:
        decision = decisions[contract]
        lines += [
            f"### 合同 `{contract}`",
            "",
            f"- 规则：{decision['rule']}",
            f"- 照片家族数 / 许可单元：**{decision['license_units']}**",
            f"- 应付费用：**{decision['total_fee']}**（{decision['license_units']} × {decision['fee_per_unit']}）",
            "",
            "| 家族 | 成员资产 | SKU | 不同 SHA 数 |",
            "| --- | --- | --- | ---: |",
        ]
        for family in decision["photo_families"]:
            lines.append(
                f"| {family['family_id']} | {', '.join(family['asset_ids'])} "
                f"| {_md(', '.join(family['skus']))} | {len(family['sha256'])} |"
            )
        lines.append("")
    lines += ["## 派生关系证据", ""]
    for edge in evidence["derivative_edges"]:
        if not edge["accepted"]:
            continue
        if edge["edge_kind"] == "exact_copy":
            lines.append(
                f"- `{edge['base_asset']}` ↔ `{edge['candidate_asset']}`：字节完全相同（exact_copy）。"
            )
            continue
        alignment = edge["best_alignment"]
        lines.append(
            f"- `{edge['base_asset']}` → `{edge['candidate_asset']}`：顺时针旋转 "
            f"{alignment['rotation_degrees_cw']}°，四边裁切 "
            f"上/右/下/左 = {alignment['crop_top_px']}/{alignment['crop_right_px']}/"
            f"{alignment['crop_bottom_px']}/{alignment['crop_left_px']} px；"
            f"主体双向覆盖率 {alignment['base_coverage']:.3f}/"
            f"{alignment['candidate_coverage']:.3f}，主体同色率 "
            f"{alignment['match_rate']:.3f}，归 {edge.get('family_id', '-')}。"
        )
    lines += ["", "## 拒合并反例（不得自动少计）", ""]
    if evidence["rejected_merges"]:
        lines += [
            "| 底图 | 候选 | 拒绝原因 | 模板背景一致率 | 主体同色率 |",
            "| --- | --- | --- | ---: | ---: |",
        ]
        for item in evidence["rejected_merges"]:
            alignment = item.get("best_alignment") or {}
            match_rate = alignment.get("match_rate")
            lines.append(
                f"| {item['base_asset']} | {item['candidate_asset']} "
                f"| `{item['reject_reason']}` | {item['background']['template_agreement']:.3f} "
                f"| {match_rate if match_rate is not None else '-'} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "> 仅在 90 度倍数旋转与有界边框裁切内、且重叠有效主体逐像素同色时合并家族；",
        "> 歧义、超界变换、像素损坏、路径越界或不可信 snapshot 一律拒绝并按独立单元处理。",
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
