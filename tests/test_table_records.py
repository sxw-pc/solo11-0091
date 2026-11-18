"""表格结构化记录：行关联、rowspan 合同、嵌套表隔离与拒发证据。"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from selregress.table_records import build_table_records, render_table_records
from selregress.util import PROJECT_ROOT, sha256_text

OFFER_MANIFEST = PROJECT_ROOT / "fixtures/table_contract/manifest.json"
EDGE_MANIFEST = PROJECT_ROOT / "fixtures/table_contract/edge-manifest.json"
MONEY_MANIFEST = PROJECT_ROOT / "fixtures/table_contract/money-manifest.json"


def _contract(result: dict, name: str) -> dict:
    matches = [item for item in result["contract_results"] if item["contract"] == name]
    assert len(matches) == 1
    return matches[0]


def _triples(contract_result: dict) -> list[tuple[str, str, str]]:
    return [
        (
            record["fields"]["group"]["normalized_value"],
            record["fields"]["item"]["normalized_value"],
            record["fields"]["amount"]["normalized_value"],
        )
        for record in contract_result["records"]
    ]


# ---------- 固定起点：offer-grid 两份合同 ----------


def test_offer_grid_carry_rowspan_publishes_four_records():
    result = build_table_records(OFFER_MANIFEST)
    carry = _contract(result, "carry_rowspan")
    assert carry["published_count"] == 4
    assert carry["rejected_count"] == 0
    assert _triples(carry) == [
        ("A", "X", "100"),
        ("A", "Y", "200"),
        ("B", "X", "120"),
        ("B", "Y", "220"),
    ]
    # 继承批次必须标明来源行，且复用源单元格的转换语义。
    inherited = [
        record
        for record in carry["records"]
        if record["fields"]["group"].get("inheritance") == "rowspan_carry"
    ]
    assert {item["fields"]["group"]["inherited_from_row"] for item in inherited} == {0, 2}


def test_offer_grid_explicit_only_publishes_two_and_keeps_row_evidence():
    result = build_table_records(OFFER_MANIFEST)
    explicit = _contract(result, "explicit_only")
    assert explicit["published_count"] == 2
    assert _triples(explicit) == [("A", "X", "100"), ("B", "X", "120")]
    assert explicit["rejected_count"] == 2
    rejected_rows = {item["row_position"]: item for item in explicit["rejected_rows"]}
    assert sorted(rejected_rows) == [1, 3]
    for row_position in (1, 3):
        rejected = rejected_rows[row_position]
        assert rejected["rejection_reasons"] == ["group:missing_explicit_group"]
        group = rejected["fields"]["group"]
        assert group["conversion_status"] == "missing_explicit_group"
        assert group["rowspan_source_row"] == row_position - 1
        # 行级来源证据保留：item/amount 仍有完整取值链路。
        assert rejected["fields"]["item"]["conversion_status"] == "ok"
        assert rejected["fields"]["amount"]["conversion_status"] == "ok"
        assert rejected["fields"]["amount"]["raw_values"]


def test_contracts_are_isolated_on_same_snapshot():
    result = build_table_records(OFFER_MANIFEST)
    carry, explicit = (_contract(result, name) for name in ("carry_rowspan", "explicit_only"))
    carry_ids = {item["record_id"] for item in carry["records"]}
    explicit_ids = {item["record_id"] for item in explicit["records"]}
    assert explicit_ids < carry_ids
    # 同一行身份在两份合同中一致，但发布决策独立。
    shared = next(iter(explicit_ids))
    explicit_record = next(item for item in explicit["records"] if item["record_id"] == shared)
    carry_record = next(item for item in carry["records"] if item["record_id"] == shared)
    assert "inheritance" not in explicit_record["fields"]["group"]
    # 显式合同不得携带继承标记，继承结果不得跨合同泄漏。
    for record in explicit["records"]:
        assert "inheritance" not in record["fields"]["group"]
    assert carry_record["record_id"] == explicit_record["record_id"]


def test_nested_note_table_is_never_a_business_record():
    result = build_table_records(OFFER_MANIFEST)
    assert result["top_level_rows"] == 4
    assert result["global_field_hits"] == {"group": 2, "item": 5, "amount": 5}
    for contract in result["contract_results"]:
        for record in contract["records"]:
            assert record["fields"]["item"]["normalized_value"] != "Z"
            assert record["fields"]["amount"]["normalized_value"] != "999"
        for rejected in contract["rejected_rows"]:
            assert rejected["fields"]["item"]["normalized_value"] != "Z"
    # 外层第四行金额剥离嵌套表后仍为 220，并留下剥离标记。
    row3 = result["row_evidence"][3]
    amount = row3["fields"]["amount"]
    assert amount["raw_values"] == ["220"]
    assert amount["source_cells"] == [{"cell_position": 1, "nested_table_stripped": True}]
    # 外层命中与全局命中可对照，证明差异来自嵌套表而非选择器改写。
    assert amount["outer_match_count"] == 4
    assert amount["global_match_count"] == 5


def test_duplicate_item_names_are_not_merged():
    result = build_table_records(OFFER_MANIFEST)
    carry = _contract(result, "carry_rowspan")
    x_records = [
        record for record in carry["records"]
        if record["fields"]["item"]["normalized_value"] == "X"
    ]
    assert len(x_records) == 2
    assert {record["fields"]["group"]["normalized_value"] for record in x_records} == {"A", "B"}
    assert {record["row_position"] for record in x_records} == {0, 2}


def test_record_identity_is_stable_and_carries_snapshot_sha():
    first = build_table_records(OFFER_MANIFEST)
    second = build_table_records(OFFER_MANIFEST)
    assert first["snapshot_sha256"] == second["snapshot_sha256"]
    assert len(first["snapshot_sha256"]) == 64
    first_ids = [row["record_id"] for row in first["row_evidence"]]
    second_ids = [row["record_id"] for row in second["row_evidence"]]
    assert first_ids == second_ids
    assert all(identity.startswith("row-") for identity in first_ids)
    # 行位置被固定保留在身份与证据中。
    for row in first["row_evidence"]:
        assert row["record_id"].startswith(f"row-{row['row_position']:04d}-")


def test_render_is_byte_stable_across_runs():
    first = render_table_records(OFFER_MANIFEST)
    second = render_table_records(OFFER_MANIFEST)
    assert first == second
    assert first.endswith("\n")
    # 稳定 JSON：键排序、无时间戳、无随机元素。
    assert json.loads(first) == json.loads(second)


def test_header_rows_are_not_published():
    result = build_table_records(OFFER_MANIFEST)
    assert result["top_level_rows"] == 4
    for contract in result["contract_results"]:
        positions = [r["row_position"] for r in contract["records"]]
        assert positions == sorted(positions)
        assert all(0 <= pos < 4 for pos in positions)


# ---------- 合成反例：edge-grid ----------


def test_edge_grid_bad_rows_do_not_block_valid_rows():
    result = build_table_records(EDGE_MANIFEST)
    carry = _contract(result, "carry_rowspan")
    # 行 0/1 有效；行 2 类型错误、行 3 空项目、行 4 缺金额，均不得牵连前两行。
    assert _triples(carry) == [("C", "X", "300"), ("C", "X", "310")]
    rejected = {item["row_position"]: item for item in carry["rejected_rows"]}
    assert sorted(rejected) == [2, 3, 4]
    assert rejected[2]["rejection_reasons"] == ["amount:type_error"]
    assert rejected[3]["rejection_reasons"] == ["item:empty"]
    assert rejected[4]["rejection_reasons"] == ["amount:no_match"]
    # 类型错误保留原始坏值与结构化错误，不抛异常。
    bad_amount = rejected[2]["fields"]["amount"]
    assert bad_amount["raw_values"] == ["oops"]
    assert bad_amount["error"]
    assert bad_amount["normalized_value"] == "oops"


def test_edge_grid_multi_level_nested_tables_are_excluded():
    result = build_table_records(EDGE_MANIFEST)
    # 全局 CSS 仍可见两层嵌套表（Z1/9001、Z2/9002），外层关联必须剔除。
    assert result["global_field_hits"]["item"] == 7
    assert result["global_field_hits"]["amount"] == 6
    row1 = result["row_evidence"][1]
    assert row1["fields"]["item"]["normalized_value"] == "X"
    assert row1["fields"]["amount"]["normalized_value"] == "310"
    assert row1["fields"]["amount"]["source_cells"][0]["nested_table_stripped"] is True
    for contract in result["contract_results"]:
        flat = [
            record["fields"][key]["normalized_value"]
            for record in contract["records"]
            for key in ("item", "amount")
        ]
        assert "Z1" not in flat and "Z2" not in flat
        assert "9001" not in flat and "9002" not in flat


def test_edge_grid_rowspan_past_table_end_is_flagged_without_fabricating_rows():
    result = build_table_records(EDGE_MANIFEST)
    codes = {issue["code"] for issue in result["grid_issues"]}
    assert "rowspan_past_table_end" in codes
    assert result["top_level_rows"] == 5
    carry = _contract(result, "carry_rowspan")
    # 越界不虚构第六行；D 实际覆盖 2/3/4 三行。
    coverage = carry["group_rowspan_coverage"]
    d_coverage = next(item for item in coverage if item["start_row"] == 2)
    assert d_coverage["covers_rows"] == [2, 3, 4]
    all_positions = [
        item["row_position"]
        for contract in result["contract_results"]
        for item in contract["records"] + contract["rejected_rows"]
    ]
    assert sorted(all_positions) == sorted(list(range(5)) * 2)


def test_edge_grid_explicit_contract_combines_missing_group_and_field_errors():
    result = build_table_records(EDGE_MANIFEST)
    explicit = _contract(result, "explicit_only")
    assert _triples(explicit) == [("C", "X", "300")]
    rejected = {item["row_position"]: item for item in explicit["rejected_rows"]}
    assert rejected[1]["rejection_reasons"] == ["group:missing_explicit_group"]
    assert rejected[3]["rejection_reasons"] == [
        "group:missing_explicit_group",
        "item:empty",
    ]
    assert rejected[4]["rejection_reasons"] == [
        "group:missing_explicit_group",
        "amount:no_match",
    ]
    # 行 2 自己写有 D 单元格，显式合同下缺批次不成立，只剩类型错误。
    assert rejected[2]["rejection_reasons"] == ["amount:type_error"]


def test_field_semantics_follow_field_spec_pipeline():
    result = build_table_records(EDGE_MANIFEST)
    amount_spec = result["field_specs"]["amount"]
    assert amount_spec["field_type"] == "integer"
    row0 = result["row_evidence"][0]
    assert row0["fields"]["amount"]["field_type"] == "integer"
    assert row0["fields"]["amount"]["conversion_status"] == "ok"
    assert row0["fields"]["amount"]["normalized_value"] == "300"
    assert row0["fields"]["amount"]["used_selector"] == "td.amount"


def test_money_grid_uses_field_spec_normalization_and_decimal_conversion():
    result = build_table_records(MONEY_MANIFEST)
    assert result["field_specs"]["amount"] == {
        "selector": "td.amount",
        "field_type": "decimal",
        "extract": "text",
        "normalize": ["trim", "strip_thousands"],
    }
    carry = _contract(result, "carry_rowspan")
    amounts = {
        record["row_position"]: record["fields"]["amount"]
        for record in carry["records"]
    }
    assert amounts[0]["raw_values"] == ["1,200.50"]
    assert amounts[0]["normalized_value"] == "1200.5"
    # 货币符号由既有 decimal 转换的数值清洗处理，千分位由归一化规则剥离。
    assert amounts[1]["raw_values"] == ["￥3,000"]
    assert amounts[1]["normalized_value"] == "3000"
    assert all(item["conversion_status"] == "ok" for item in amounts.values())


# ---------- 输入变更：重算或拒发，不沿用旧记录 ----------


def test_changed_html_changes_sha_and_record_evidence(workspace: Path):
    manifest = json.loads(OFFER_MANIFEST.read_text(encoding="utf-8"))
    html = (PROJECT_ROOT / manifest["snapshot"]).read_text(encoding="utf-8")
    snapshot_path = workspace / "snapshot.html"
    manifest_path = workspace / "manifest.json"
    manifest = copy.deepcopy(manifest)
    manifest["snapshot"] = str(snapshot_path)
    snapshot_path.write_text(html, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    baseline = build_table_records(manifest_path, root=PROJECT_ROOT)
    tampered = html.replace(">220<", ">221<")
    snapshot_path.write_text(tampered, encoding="utf-8")
    recomputed = build_table_records(manifest_path, root=PROJECT_ROOT)

    assert recomputed["snapshot_sha256"] != baseline["snapshot_sha256"]
    assert recomputed["snapshot_sha256"] == sha256_text(tampered)
    carry = _contract(recomputed, "carry_rowspan")
    row3 = next(record for record in carry["records"] if record["row_position"] == 3)
    assert row3["fields"]["amount"]["normalized_value"] == "221"


def test_row_count_mismatch_is_rejected_not_replayed(workspace: Path):
    from selregress.util import InputError

    manifest = json.loads(OFFER_MANIFEST.read_text(encoding="utf-8"))
    html = (PROJECT_ROOT / manifest["snapshot"]).read_text(encoding="utf-8")
    snapshot_path = workspace / "snapshot.html"
    manifest_path = workspace / "manifest.json"
    manifest = copy.deepcopy(manifest)
    manifest["snapshot"] = str(snapshot_path)
    # 删除一条外层业务行，但仍声明 4 行：必须拒发。
    snapshot_path.write_text(
        html.replace(
            '<tr><td class="item">Y</td><td class="amount">200</td></tr>\n', ""
        ),
        encoding="utf-8",
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputError) as caught:
        build_table_records(manifest_path, root=PROJECT_ROOT)
    assert caught.value.code == "TOP_LEVEL_ROW_MISMATCH"


def test_selector_change_recomputes_evidence_and_fingerprint(workspace: Path):
    manifest = json.loads(OFFER_MANIFEST.read_text(encoding="utf-8"))
    html = (PROJECT_ROOT / manifest["snapshot"]).read_text(encoding="utf-8")
    snapshot_path = workspace / "snapshot.html"
    manifest_path = workspace / "manifest.json"
    manifest = copy.deepcopy(manifest)
    manifest["snapshot"] = str(snapshot_path)
    snapshot_path.write_text(html, encoding="utf-8")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    baseline = build_table_records(manifest_path, root=PROJECT_ROOT)

    manifest["item_selector"] = "td.item.missing"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    recomputed = build_table_records(manifest_path, root=PROJECT_ROOT)
    assert recomputed["manifest_fingerprint"] != baseline["manifest_fingerprint"]
    carry = _contract(recomputed, "carry_rowspan")
    assert carry["published_count"] == 0
    assert {
        reason
        for row in carry["rejected_rows"]
        for reason in row["rejection_reasons"]
    } == {"item:no_match"}


def test_unknown_contract_is_rejected(workspace: Path):
    from selregress.util import InputError

    manifest = json.loads(OFFER_MANIFEST.read_text(encoding="utf-8"))
    manifest_path = workspace / "manifest.json"
    manifest = copy.deepcopy(manifest)
    manifest["contracts"] = ["carry_rowspan", "silent_merge"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputError) as caught:
        build_table_records(manifest_path, root=PROJECT_ROOT)
    assert caught.value.code == "MANIFEST_INVALID"
    assert "silent_merge" in caught.value.message


def test_manifest_outside_root_is_rejected(workspace: Path):
    from selregress.util import InputError

    # workspace 位于 tests/_tmp/cases 之下，其父目录仍在项目根内；
    # 改用系统临时目录构造明确越界的清单路径。
    import tempfile

    with tempfile.TemporaryDirectory() as temp_dir:
        outside = Path(temp_dir) / "outside-manifest.json"
        outside.write_text("{}", encoding="utf-8")
        with pytest.raises(InputError) as caught:
            build_table_records(outside, root=PROJECT_ROOT)
        assert caught.value.code == "PATH_OUTSIDE_ROOT"


def test_invalid_manifest_field_config_is_rejected(workspace: Path):
    from selregress.util import InputError

    base = json.loads(OFFER_MANIFEST.read_text(encoding="utf-8"))
    html = (PROJECT_ROOT / base["snapshot"]).read_text(encoding="utf-8")
    snapshot_path = workspace / "snapshot.html"
    snapshot_path.write_text(html, encoding="utf-8")

    bad_type = copy.deepcopy(base)
    bad_type["snapshot"] = str(snapshot_path)
    bad_type["fields"] = {"amount": {"type": "matrix"}}
    manifest_path = workspace / "bad-type.json"
    manifest_path.write_text(json.dumps(bad_type), encoding="utf-8")
    with pytest.raises(InputError) as caught:
        build_table_records(manifest_path, root=PROJECT_ROOT)
    assert caught.value.code == "MANIFEST_INVALID"

    bad_extract = copy.deepcopy(base)
    bad_extract["snapshot"] = str(snapshot_path)
    bad_extract["fields"] = {"group": {"extract": "attr"}}
    manifest_path = workspace / "bad-extract.json"
    manifest_path.write_text(json.dumps(bad_extract), encoding="utf-8")
    with pytest.raises(InputError) as caught:
        build_table_records(manifest_path, root=PROJECT_ROOT)
    assert caught.value.code == "MANIFEST_INVALID"

    bad_rows = copy.deepcopy(base)
    bad_rows["snapshot"] = str(snapshot_path)
    bad_rows["expected_top_level_rows"] = True
    manifest_path = workspace / "bad-rows.json"
    manifest_path.write_text(json.dumps(bad_rows), encoding="utf-8")
    with pytest.raises(InputError) as caught:
        build_table_records(manifest_path, root=PROJECT_ROOT)
    assert caught.value.code == "MANIFEST_INVALID"


def test_carry_invalid_group_source_is_not_silently_inherited(workspace: Path):
    # 起始行批次单元格为空：carry 合同不得把空批次伪装成有效值继承下去。
    html = (
        '<!doctype html><table class="g"><tbody>'
        '<tr><th class="group" rowspan="2"> </th><td class="item">X</td>'
        '<td class="amount">10</td></tr>'
        '<tr><td class="item">Y</td><td class="amount">20</td></tr>'
        "</tbody></table>"
    )
    snapshot_path = workspace / "snapshot.html"
    snapshot_path.write_text(html, encoding="utf-8")
    manifest = {
        "snapshot": str(snapshot_path),
        "table_selector": "table.g",
        "group_selector": "th.group",
        "item_selector": "td.item",
        "amount_selector": "td.amount",
        "contracts": ["carry_rowspan", "explicit_only"],
        "expected_top_level_rows": 2,
    }
    manifest_path = workspace / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = build_table_records(manifest_path, root=PROJECT_ROOT)
    carry = _contract(result, "carry_rowspan")
    assert carry["published_count"] == 0
    rejected = {item["row_position"]: item for item in carry["rejected_rows"]}
    assert any(reason.startswith("group:") for reason in rejected[0]["rejection_reasons"])
    inherited_group = rejected[1]["fields"]["group"]
    assert inherited_group["conversion_status"] == "inherited_source_invalid"
    assert inherited_group["inherited_from_row"] == 0
    # 其他字段的证据仍然保留，没有因批次问题整体消失。
    assert rejected[1]["fields"]["amount"]["normalized_value"] == "20"


# ---------- CLI 接入 ----------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-m", "selregress", *args],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_table_records_cli_outputs_stable_json():
    result = _cli("table-records", "--manifest", "fixtures/table_contract/manifest.json")
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    carry = _contract(payload, "carry_rowspan")
    assert _triples(carry) == [
        ("A", "X", "100"),
        ("A", "Y", "200"),
        ("B", "X", "120"),
        ("B", "Y", "220"),
    ]
    second = _cli("table-records", "--manifest", "fixtures/table_contract/manifest.json")
    assert second.stdout == result.stdout


def test_table_records_cli_rejects_bad_manifest_with_exit_three(workspace: Path):
    manifest_path = workspace / "bad.json"
    manifest_path.write_text("{not-json", encoding="utf-8")
    result = _cli("table-records", "--manifest", str(manifest_path.relative_to(PROJECT_ROOT)))
    assert result.returncode == 3
    payload = json.loads(result.stderr)
    assert payload["error"] == "MANIFEST_INVALID"


def test_table_evidence_command_remains_fixed_starting_point():
    result = _cli("table-evidence", "--manifest", "fixtures/table_contract/manifest.json")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["records"] is None
    assert payload["top_level_rows"] == 4
    assert payload["field_hits"] == {"group_selector": 2, "item_selector": 5, "amount_selector": 5}
