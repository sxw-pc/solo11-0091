"""把同一外层表格明细行的字段关联为结构化记录，并按声明合同决定行归属。

与 ``table_evidence`` 的区别：本模块不再把三个字段当成互不相关的全局
CSS 命中数组，而是沿外层表格的真实行结构关联字段：

- 嵌套 ``table``（含注释表）内的行与单元格不属于外层业务明细，永不发布；
- ``thead`` 中的表头行不作为业务明细；
- 字段仍走 :class:`~selregress.models.FieldSpec`、``matcher`` 与
  ``extractor`` 的既有取值、归一化与类型转换语义；
- 消费者在清单中事先声明行归属合同（``carry_rowspan`` 或
  ``explicit_only``），两份合同在同一快照上独立计算，互不继承结果。

输出只依赖本地有界 HTML 与清单内容，同输入重复运行字节稳定。
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .extractor import extract_field
from .matcher import match_nodes
from .models import FieldSpec, NodeSelection
from .util import (
    InputError,
    PROJECT_ROOT,
    canonical_digest,
    relative_posix,
    resolve_under_root,
    sha256_file,
    stable_json_text,
)

SUPPORTED_CONTRACTS = ("carry_rowspan", "explicit_only")
FIELD_KEYS = ("group", "item", "amount")
DEFAULT_FIELD_TYPES = {
    "group": "string",
    "item": "string",
    "amount": "integer",
}
MANIFEST_SELECTOR_KEYS = (
    "table_selector",
    "group_selector",
    "item_selector",
    "amount_selector",
)
VALID_FIELD_TYPES = {"string", "integer", "decimal", "date", "url", "boolean"}
VALID_EXTRACT_MODES = {"text", "attr", "href", "src", "html"}


def load_table_manifest(
    manifest_path: str | Path, root: Path = PROJECT_ROOT
) -> tuple[dict[str, Any], Path, Path]:
    """读取并静态校验表格合同清单，返回 (清单, 快照绝对路径, 项目根)。"""
    root = root.resolve()
    manifest_file = resolve_under_root(manifest_path, root, "manifest")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError("MANIFEST_INVALID", f"清单无法解析: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InputError("MANIFEST_INVALID", "清单根节点必须是 JSON 对象")
    for key in ("snapshot", *MANIFEST_SELECTOR_KEYS, "contracts", "expected_top_level_rows"):
        if key not in manifest:
            raise InputError("MANIFEST_INVALID", f"清单缺少必需键: {key}")
    for key in MANIFEST_SELECTOR_KEYS:
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise InputError("MANIFEST_INVALID", f"{key} 必须是非空字符串")
    snapshot = resolve_under_root(manifest["snapshot"], root, "snapshot")
    if not snapshot.is_file():
        raise InputError("SNAPSHOT_NOT_FOUND", f"快照不存在: {manifest['snapshot']}")
    contracts = manifest["contracts"]
    if not isinstance(contracts, list) or not contracts or any(
        not isinstance(item, str) for item in contracts
    ):
        raise InputError("MANIFEST_INVALID", "contracts 必须是非空字符串数组")
    unknown = sorted(set(contracts) - set(SUPPORTED_CONTRACTS))
    if unknown:
        raise InputError("MANIFEST_INVALID", f"未知表格合同: {', '.join(unknown)}")
    if len(contracts) != len(set(contracts)):
        raise InputError("MANIFEST_INVALID", "contracts 存在重复声明")
    if not isinstance(manifest["expected_top_level_rows"], int) or isinstance(
        manifest["expected_top_level_rows"], bool
    ):
        raise InputError("MANIFEST_INVALID", "expected_top_level_rows 必须是整数")
    if manifest["expected_top_level_rows"] < 0:
        raise InputError("MANIFEST_INVALID", "expected_top_level_rows 不能为负数")
    fields_config = manifest.get("fields", {})
    if fields_config is None:
        fields_config = {}
    if not isinstance(fields_config, dict):
        raise InputError("MANIFEST_INVALID", "fields 必须是对象")
    return manifest, snapshot, root


def _build_field_specs(manifest: dict[str, Any]) -> dict[str, FieldSpec]:
    fields_config = manifest.get("fields") or {}
    specs: dict[str, FieldSpec] = {}
    for key in FIELD_KEYS:
        config = fields_config.get(key) or {}
        if not isinstance(config, dict):
            raise InputError("MANIFEST_INVALID", f"fields.{key} 必须是对象")
        field_type = config.get("type", DEFAULT_FIELD_TYPES[key])
        if field_type not in VALID_FIELD_TYPES:
            raise InputError("MANIFEST_INVALID", f"fields.{key}.type 非法: {field_type}")
        normalize = config.get("normalize", ())
        if normalize is None:
            normalize = ()
        if not isinstance(normalize, (list, tuple)) or any(
            not isinstance(item, (str, dict)) for item in normalize
        ):
            raise InputError("MANIFEST_INVALID", f"fields.{key}.normalize 必须是字符串/映射数组")
        extract = config.get("extract", "text")
        if extract not in VALID_EXTRACT_MODES:
            raise InputError("MANIFEST_INVALID", f"fields.{key}.extract 非法: {extract}")
        if extract == "attr" and not isinstance(config.get("attribute"), str):
            raise InputError("MANIFEST_INVALID", f"fields.{key} 使用 attr 取值时必须提供 attribute")
        specs[key] = FieldSpec(
            name=key,
            selectors=(manifest[f"{key}_selector"],),
            field_type=field_type,
            extract=extract,
            attribute=config.get("attribute"),
            normalize=tuple(normalize),
        )
    return specs


def _outer_rows(table: Any) -> list[Any]:
    """仅取外层表自身的 tr，排除嵌套表行与 thead 表头行。"""
    return [
        row
        for row in table.select("tr")
        if row.find_parent("table") is table and row.find_parent("thead") is None
    ]


def _outer_cells(row: Any, table: Any) -> list[Any]:
    """仅取该行直接属于外层表的单元格，嵌套表内单元格被排除。"""
    return [
        cell
        for cell in row.find_all(["th", "td"], recursive=False)
        if cell.find_parent("table") is table
    ]


def _int_span(value: Any) -> int:
    try:
        span = int(value)
    except (TypeError, ValueError):
        return 1
    return span if span >= 1 else 1


def build_row_grid(
    rows: list[Any], table: Any
) -> tuple[dict[int, list[dict[str, Any]]], list[dict[str, Any]]]:
    """按 HTML rowspan/colspan 语义把外层单元格铺到行网格。

    返回 (row_index -> 该行网格单元列表, 结构问题列表)。每个网格单元记录
    外层单元格 ``cell``、它在源行中的 ``cell_position`` 与 ``start_row``。
    rowspan 越过实际表尾时不虚构行，只产生结构问题。
    """
    occupied: dict[tuple[int, int], dict[str, Any]] = {}
    pending: dict[int, tuple[Any, int, int]] = {}  # 列 -> (单元格, 起始行, 剩余跨行数)
    issues: list[dict[str, Any]] = []
    grid: dict[int, list[dict[str, Any]]] = {}
    for row_index, row in enumerate(rows):
        entries: list[dict[str, Any]] = []
        # 先放入仍在生效的上方 rowspan 单元格。
        for col in sorted(pending):
            cell, start_row, remaining = pending[col]
            entry = occupied[(start_row, col)]
            if (row_index, col) in occupied:
                issues.append(
                    {
                        "code": "grid_rowspan_overlap",
                        "row_position": row_index,
                        "cell_position": col,
                        "message": f"外层行 {row_index} 与起始行 {start_row} 的 rowspan 单元格冲突",
                    }
                )
                del pending[col]
                continue
            occupied[(row_index, col)] = entry
            entries.append(entry)
            if remaining <= 1:
                del pending[col]
            else:
                pending[col] = (cell, start_row, remaining - 1)
        # 再放入本行自有单元格，跳过已被上方 rowspan 占据的列。
        column = 0
        for cell_position, cell in enumerate(_outer_cells(row, table)):
            while (row_index, column) in occupied:
                column += 1
            rowspan = _int_span(cell.get("rowspan"))
            colspan = _int_span(cell.get("colspan"))
            entry = {"cell": cell, "cell_position": cell_position, "start_row": row_index}
            for offset in range(colspan):
                target = (row_index, column + offset)
                if target in occupied:
                    issues.append(
                        {
                            "code": "grid_cell_overlap",
                            "row_position": row_index,
                            "cell_position": cell_position,
                            "message": f"外层行 {row_index} 单元格 {cell_position} 与已有网格重叠",
                        }
                    )
                occupied[target] = entry
                entries.append(entry)
                if rowspan > 1:
                    pending[column + offset] = (cell, row_index, rowspan - 1)
            column += colspan
        entries.sort(key=lambda item: (item["start_row"], item["cell_position"]))
        grid[row_index] = entries
    for col, (cell, start_row, remaining) in sorted(pending.items()):
        issues.append(
            {
                "code": "rowspan_past_table_end",
                "row_position": start_row,
                "cell_position": col,
                "message": f"起始行 {start_row} 的 rowspan 超出实际表尾 {remaining} 行",
            }
        )
    return grid, issues


def _scoped_selection(table: Any, spec: FieldSpec) -> tuple[NodeSelection, int]:
    """在既有全局 CSS 匹配之上，只保留直接属于外层表的节点。

    选择器配方本身不被改写；嵌套表内节点在此被结构性剔除。
    返回 (过滤后的 NodeSelection, 全局原始命中数)。
    """
    selection = match_nodes(table, spec)
    global_count = selection.count
    nodes = [node for node in selection.nodes if node.find_parent("table") is table]
    selector_hits: list[dict[str, Any]] = []
    for hit in selection.selector_hits:
        if hit["selector"] == selection.used_selector:
            hit = dict(hit)
            hit["global_count"] = hit["count"]
            hit["count"] = len(nodes)
        selector_hits.append(hit)
    return (
        NodeSelection(
            nodes=nodes,
            selector_hits=selector_hits,
            used_selector=selection.used_selector,
            ambiguous=False,
            ambiguity_detail="",
        ),
        global_count,
    )


def _sanitized_cell(cell: Any) -> Any:
    """深拷贝单元格并移除其中的嵌套 table，使注释表不污染外层字段取值。"""
    clone = copy.deepcopy(cell)
    for nested in clone.find_all("table"):
        nested.decompose()
    return clone


def _field_evidence(
    spec: FieldSpec,
    scoped: NodeSelection,
    global_count: int,
    row_cell_ids: set[int],
    cell_positions: dict[int, int],
) -> dict[str, Any]:
    """对单行的某一字段做匹配、取值、归一化与类型转换，返回行级证据。"""
    nodes = [node for node in scoped.nodes if id(node) in row_cell_ids]
    sanitized = [_sanitized_cell(node) for node in nodes]
    selection = NodeSelection(
        nodes=sanitized,
        selector_hits=scoped.selector_hits,
        used_selector=scoped.used_selector,
        ambiguous=False,
        ambiguity_detail="",
    )
    extraction = extract_field(selection, spec)
    if not nodes:
        status = "no_match"
    elif len(nodes) > 1:
        status = "multi_match"
    else:
        status = extraction.conversion_status
    return {
        "field": spec.name,
        "field_type": spec.field_type,
        "selector": spec.selectors[0],
        "used_selector": extraction.used_selector,
        "global_match_count": global_count,
        "outer_match_count": scoped.count,
        "row_match_count": len(nodes),
        "conversion_status": status,
        "raw_values": extraction.raw_values,
        "normalized_values": extraction.normalized_values,
        "normalized_value": extraction.normalized_value,
        "error": extraction.error,
        "source_cells": [
            {
                "cell_position": cell_positions[id(node)],
                "nested_table_stripped": bool(node.find_all("table")),
            }
            for node in nodes
        ],
    }


def _record_id(snapshot_rel: str, table_selector: str, row_position: int) -> str:
    digest = canonical_digest(
        {"row_position": row_position, "snapshot": snapshot_rel, "table": table_selector}
    )[:16]
    return f"row-{row_position:04d}-{digest}"


def _rejection_reasons(fields: dict[str, dict[str, Any]]) -> list[str]:
    return [
        f"{key}:{fields[key]['conversion_status']}"
        for key in FIELD_KEYS
        if fields[key]["conversion_status"] != "ok"
    ]


def _evaluate_contract(
    contract: str,
    row_count: int,
    grid: dict[int, list[dict[str, Any]]],
    row_evidence: dict[int, dict[str, Any]],
    allowed_group_nodes: set[int],
    snapshot_rel: str,
    table_selector: str,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    group_coverage: dict[int, list[int]] = {}
    for row_index in range(row_count):
        own_group_cells = {
            id(entry["cell"])
            for entry in grid.get(row_index, [])
            if entry["start_row"] == row_index and id(entry["cell"]) in allowed_group_nodes
        }
        for cell_id in own_group_cells:
            group_coverage[row_index] = sorted(
                row_idx
                for row_idx, entries in grid.items()
                if any(id(entry["cell"]) == cell_id for entry in entries)
            )

    for row_index in range(row_count):
        fields = copy.deepcopy(row_evidence[row_index]["fields"])
        record_id = _record_id(snapshot_rel, table_selector, row_index)
        own_group = [
            entry
            for entry in grid.get(row_index, [])
            if entry["start_row"] == row_index and id(entry["cell"]) in allowed_group_nodes
        ]
        if contract == "explicit_only":
            if not own_group:
                # 本行没有自己书写的批次单元格；若上方有 rowspan 覆盖，
                # 保留该来源作为行级证据，但不计为显式批次。
                covering_cells = [
                    entry
                    for entry in grid.get(row_index, [])
                    if entry["start_row"] != row_index and id(entry["cell"]) in allowed_group_nodes
                ]
                fields["group"] = copy.deepcopy(fields["group"])
                fields["group"]["conversion_status"] = "missing_explicit_group"
                fields["group"]["row_match_count"] = 0
                if covering_cells:
                    fields["group"]["rowspan_source_row"] = covering_cells[0]["start_row"]
                fields["group"]["error"] = None
        elif not own_group:
            # carry_rowspan：只接受源表 rowspan 显式覆盖到本行的批次单元格。
            covering = [
                entry
                for entry in grid.get(row_index, [])
                if entry["start_row"] != row_index and id(entry["cell"]) in allowed_group_nodes
            ]
            if covering:
                source = covering[0]
                source_position = source["start_row"]
                source_group = copy.deepcopy(row_evidence[source_position]["fields"]["group"])
                source_group["inherited_from_row"] = source_position
                source_group["inheritance"] = "rowspan_carry"
                if source_group["conversion_status"] != "ok":
                    source_group["conversion_status"] = "inherited_source_invalid"
                fields["group"] = source_group
        reasons = _rejection_reasons(fields)
        if reasons:
            rejected.append(
                {
                    "record_id": record_id,
                    "row_position": row_index,
                    "decision": "rejected",
                    "rejection_reasons": reasons,
                    "fields": fields,
                }
            )
            continue
        records.append(
            {
                "record_id": record_id,
                "row_position": row_index,
                "decision": "published",
                "fields": fields,
            }
        )
    return {
        "contract": contract,
        "published_count": len(records),
        "rejected_count": len(rejected),
        "records": records,
        "rejected_rows": rejected,
        "group_rowspan_coverage": [
            {"start_row": start, "covers_rows": covered}
            for start, covered in sorted(group_coverage.items())
        ],
    }


def build_table_records(manifest_path: str | Path, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """从本地快照与清单计算表格结构化记录证据。"""
    manifest, snapshot, root = load_table_manifest(manifest_path, root)
    snapshot_rel = relative_posix(snapshot, root)
    snapshot_sha = sha256_file(snapshot)
    manifest_fingerprint = canonical_digest(manifest)
    soup = BeautifulSoup(snapshot.read_text(encoding="utf-8"), "html.parser")
    tables = soup.select(manifest["table_selector"])
    if len(tables) != 1:
        raise InputError(
            "TABLE_SELECTOR_AMBIGUOUS",
            f"table_selector 必须唯一命中本地表格，实际命中 {len(tables)} 个",
        )
    table = tables[0]
    rows = _outer_rows(table)
    if len(rows) != manifest["expected_top_level_rows"]:
        raise InputError(
            "TOP_LEVEL_ROW_MISMATCH",
            f"外层明细行数 {len(rows)} 与清单声明 {manifest['expected_top_level_rows']} 不一致，拒发",
        )
    specs = _build_field_specs(manifest)

    # 全局命中保留为证据（与 table_evidence 的固定起点一致），
    # 但记录关联只使用直接属于外层表的节点。
    global_hits: dict[str, int] = {}
    scoped_by_field: dict[str, NodeSelection] = {}
    allowed_by_field: dict[str, set[int]] = {}
    for key in FIELD_KEYS:
        scoped, global_count = _scoped_selection(table, specs[key])
        global_hits[key] = global_count
        scoped_by_field[key] = scoped
        allowed_by_field[key] = {id(node) for node in scoped.nodes}

    grid, grid_issues = build_row_grid(rows, table)
    row_evidence: dict[int, dict[str, Any]] = {}
    for row_index, row in enumerate(rows):
        entries = grid.get(row_index, [])
        cell_positions = {
            id(entry["cell"]): entry["cell_position"] for entry in entries
        }
        cells_by_field: dict[str, set[int]] = {key: set() for key in FIELD_KEYS}
        for entry in entries:
            for key in FIELD_KEYS:
                if id(entry["cell"]) in allowed_by_field[key]:
                    cells_by_field[key].add(id(entry["cell"]))
        fields = {
            key: _field_evidence(
                specs[key],
                scoped_by_field[key],
                global_hits[key],
                cells_by_field[key],
                cell_positions,
            )
            for key in FIELD_KEYS
        }
        row_evidence[row_index] = {
            "record_id": _record_id(snapshot_rel, manifest["table_selector"], row_index),
            "row_position": row_index,
            "outer_cell_count": len(_outer_cells(row, table)),
            "fields": fields,
        }

    contract_results = [
        _evaluate_contract(
            contract,
            len(rows),
            grid,
            row_evidence,
            allowed_by_field["group"],
            snapshot_rel,
            manifest["table_selector"],
        )
        for contract in manifest["contracts"]
    ]
    return {
        "snapshot": snapshot_rel,
        "snapshot_sha256": snapshot_sha,
        "manifest_fingerprint": manifest_fingerprint,
        "table_selector": manifest["table_selector"],
        "top_level_rows": len(rows),
        "global_field_hits": global_hits,
        "grid_issues": grid_issues,
        "field_specs": {
            key: {
                "selector": specs[key].selectors[0],
                "field_type": specs[key].field_type,
                "extract": specs[key].extract,
                "normalize": list(specs[key].normalize),
            }
            for key in FIELD_KEYS
        },
        "row_evidence": [row_evidence[index] for index in sorted(row_evidence)],
        "contract_results": contract_results,
    }


def render_table_records(manifest_path: str | Path, root: Path = PROJECT_ROOT) -> str:
    """稳定 JSON 文本，同输入重复运行字节一致。"""
    return stable_json_text(build_table_records(manifest_path, root))
