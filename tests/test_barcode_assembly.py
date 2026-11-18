from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from selregress.barcode_assembly import (
    CONTRACTS,
    BarcodeAssemblyError,
    adjudicate_assembly,
    evidence_digest,
    main as assembly_main,
    verify_report,
)
from selregress.barcode_evidence import (
    BarcodeEvidenceError,
    inspect_barcode_snapshot,
    preview_candidate_assembly,
)
from selregress.util import PROJECT_ROOT, stable_json_text

FIXTURE = PROJECT_ROOT / "fixtures" / "barcode"
LOT01 = FIXTURE / "detail-2026-01.html"
LOT02 = FIXTURE / "detail-2026-02.html"
LOT03 = FIXTURE / "detail-2026-03.html"


@pytest.fixture()
def lot01() -> dict:
    return inspect_barcode_snapshot(LOT01, version="2026-01")


@pytest.fixture()
def lot02() -> dict:
    return inspect_barcode_snapshot(LOT02, version="2026-02")


@pytest.fixture()
def lot03() -> dict:
    return inspect_barcode_snapshot(LOT03, version="2026-03")


def _mutated(evidence: dict, **changes) -> dict:
    data = copy.deepcopy(evidence)
    data.update(changes)
    data["fragments"] = [dict(row) for row in data["fragments"]]
    data["required_length"] = 1 + sum(row["count"] for row in data["fragments"])
    return data


def _synthetic(*, start: str, end: str, marker: str, fragments: list[tuple[str, int]]) -> dict:
    return {
        "barcode_id": "SYNTH-01",
        "snapshot_path": "synthetic",
        "snapshot_sha256": "0" * 64,
        "start": start,
        "end": end,
        "marker": marker,
        "fragments": [{"fragment": text, "count": count} for text, count in fragments],
        "required_length": 1 + sum(count for _, count in fragments),
        "possible_marker_decision": None,
        "guaranteed_marker_decision": None,
    }


def test_lot01_contracts_disagree_with_receipts(lot01: dict) -> None:
    report = adjudicate_assembly(lot01)
    assert report["complete_assembly_exists"] is True
    possible = report["contracts"]["possible_marker"]
    guaranteed = report["contracts"]["guaranteed_marker"]
    # 同一输入上，两份合同对“该条码含 CAG”给出相反结论
    assert possible["decision"] is True
    assert guaranteed["decision"] is False
    witness = possible["witness"]
    assert witness["assembled"] == "ACAGA"
    assert witness["contains_marker"] is True
    assert witness["usage_matches_evidence"] is True
    assert witness["fragment_usage"] == {"AC": 1, "AG": 1, "CA": 1, "GA": 1}
    counterexample = guaranteed["counterexample"]
    assert counterexample["assembled"] == "AGACA"
    assert counterexample["contains_marker"] is False
    assert counterexample["usage_matches_evidence"] is True
    # 收据可被证据预览独立复核
    assert preview_candidate_assembly(lot01, witness["assembled"])["valid"] is True
    assert preview_candidate_assembly(lot01, counterexample["assembled"])["valid"] is True
    # 裁决不改动证据本身的空决策
    assert lot01["possible_marker_decision"] is None
    assert lot01["guaranteed_marker_decision"] is None


def test_lot02_eighteen_edges_repeat_the_same_trap(lot02: dict) -> None:
    report = adjudicate_assembly(lot02)
    assert report["edge_count"] == 18
    possible = report["contracts"]["possible_marker"]
    guaranteed = report["contracts"]["guaranteed_marker"]
    assert possible["decision"] is True
    assert guaranteed["decision"] is False
    witness = possible["witness"]
    counterexample = guaranteed["counterexample"]
    assert witness["length"] == counterexample["length"] == 19
    assert witness["contains_marker"] is True
    assert counterexample["contains_marker"] is False
    # 重复次数逐一扣减后的完整用量收据
    expected = {"AC": 3, "AG": 3, "AT": 3, "CA": 3, "GA": 3, "TA": 3}
    assert witness["fragment_usage"] == expected
    assert counterexample["fragment_usage"] == expected
    assert preview_candidate_assembly(lot02, witness["assembled"])["valid"] is True
    assert preview_candidate_assembly(lot02, counterexample["assembled"])["valid"] is True
    # 按计数向量搜索，而不是排列 18 条边（18! 量级）
    assert report["search"]["with_marker"]["states_explored"] < 5000
    assert report["search"]["avoid_marker"]["states_explored"] < 5000


def test_lot03_universal_proof_not_single_candidate(lot03: dict) -> None:
    report = adjudicate_assembly(lot03)
    guaranteed = report["contracts"]["guaranteed_marker"]
    assert guaranteed["decision"] is True
    assert guaranteed["reason"] == "all_assemblies_contain_marker"
    assert guaranteed["counterexample"] is None
    certificate = guaranteed["certificate"]
    assert certificate["certificate_type"] == "search_exhaustion"
    assert certificate["claim"] == "no_complete_assembly_avoids_marker"
    assert certificate["states_explored"] > 0
    assert len(certificate["state_digest"]) == 64
    # 结构性说明：CA 到 A 后唯一出边是 AG，且组装以 C 结束
    notes = " ".join(certificate["structural_summary"])
    assert "CA" in notes and "AG" in notes and "CAG" in notes
    possible = report["contracts"]["possible_marker"]
    assert possible["decision"] is True
    witness_preview = preview_candidate_assembly(lot03, possible["witness"]["assembled"])
    assert witness_preview["valid"] is True
    assert witness_preview["contains_marker"] is True


def test_removed_fragment_leaves_no_complete_assembly(lot01: dict) -> None:
    broken = _mutated(
        lot01, fragments=[row for row in lot01["fragments"] if row["fragment"] != "GA"]
    )
    report = adjudicate_assembly(broken)
    assert report["complete_assembly_exists"] is False
    for name in CONTRACTS:
        block = report["contracts"][name]
        assert block["decision"] is False
        assert block["reason"] == "no_complete_assembly"
    # 空集合的全称真不得被误报为条码已验证
    assert report["contracts"]["guaranteed_marker"]["vacuous_truth_guard"] is True
    assert report["no_assembly_certificate"]["claim"] == "no_complete_assembly_exists"


def test_skewed_count_breaks_euler_balance(lot01: dict) -> None:
    skewed = _mutated(
        lot01,
        fragments=[
            {"fragment": "AC", "count": 2},
            {"fragment": "AG", "count": 1},
            {"fragment": "CA", "count": 1},
            {"fragment": "GA", "count": 1},
        ],
    )
    report = adjudicate_assembly(skewed)
    assert report["complete_assembly_exists"] is False
    assert report["contracts"]["possible_marker"]["decision"] is False
    assert report["contracts"]["guaranteed_marker"]["decision"] is False


def test_lot03_missing_edge_leaves_no_complete_assembly(lot03: dict) -> None:
    broken = _mutated(
        lot03,
        fragments=[dict(row, count=1) if row["fragment"] == "TC" else row for row in lot03["fragments"]],
    )
    report = adjudicate_assembly(broken)
    assert report["complete_assembly_exists"] is False
    assert report["contracts"]["guaranteed_marker"]["decision"] is False
    assert report["contracts"]["possible_marker"]["decision"] is False


def test_inflated_multiplicity_stays_tractable(lot01: dict) -> None:
    inflated = _mutated(
        lot01, fragments=[{"fragment": f, "count": 25} for f in ("AC", "AG", "CA", "GA")]
    )
    report = adjudicate_assembly(inflated)
    assert report["edge_count"] == 100
    assert report["contracts"]["possible_marker"]["decision"] is True
    guaranteed = report["contracts"]["guaranteed_marker"]
    assert guaranteed["decision"] is False
    counterexample = guaranteed["counterexample"]
    assert counterexample["length"] == 101
    assert counterexample["contains_marker"] is False
    assert counterexample["usage_matches_evidence"] is True
    assert preview_candidate_assembly(inflated, counterexample["assembled"])["valid"] is True


def test_inflated_lot03_remains_guaranteed(lot03: dict) -> None:
    inflated = _mutated(lot03, fragments=[dict(row, count=6) for row in lot03["fragments"]])
    report = adjudicate_assembly(inflated)
    assert report["edge_count"] == 30
    guaranteed = report["contracts"]["guaranteed_marker"]
    assert guaranteed["decision"] is True
    assert guaranteed["certificate"]["claim"] == "no_complete_assembly_avoids_marker"


def test_changed_anchors_flip_guaranteed_decision(lot01: dict) -> None:
    changed = _mutated(lot01, start="C", end="C")
    report = adjudicate_assembly(changed)
    assert report["complete_assembly_exists"] is True
    assert report["contracts"]["guaranteed_marker"]["decision"] is True
    assert report["contracts"]["possible_marker"]["decision"] is True
    witness = report["contracts"]["possible_marker"]["witness"]
    assert witness["assembled"].startswith("C") and witness["assembled"].endswith("C")


def test_mismatched_anchors_leave_no_complete_assembly(lot02: dict) -> None:
    changed = _mutated(lot02, start="A", end="T")
    report = adjudicate_assembly(changed)
    assert report["complete_assembly_exists"] is False
    assert report["contracts"]["guaranteed_marker"]["decision"] is False
    assert report["contracts"]["possible_marker"]["decision"] is False


def test_marker_spanning_repeat_boundary_is_detected() -> None:
    evidence = _synthetic(
        start="A", end="A", marker="CAG",
        fragments=[("AG", 2), ("CA", 2), ("GC", 2)],
    )
    candidate = "AGCAGCA"  # CAG 横跨两个 "AGC" 周期的交界
    preview = preview_candidate_assembly(evidence, candidate)
    assert preview["valid"] is True
    assert preview["contains_marker"] is True
    report = adjudicate_assembly(evidence)
    assert report["contracts"]["possible_marker"]["decision"] is True
    assert report["contracts"]["guaranteed_marker"]["decision"] is True


def test_local_edges_do_not_imply_marker_adjacency() -> None:
    # CA 与 AG 都出现，但任何完整组装里二者都不相邻：不能凭局部出现直接判含标记
    evidence = _synthetic(
        start="A", end="A", marker="CAG",
        fragments=[("AG", 1), ("AT", 1), ("CA", 1), ("GC", 1), ("TA", 1)],
    )
    report = adjudicate_assembly(evidence)
    possible = report["contracts"]["possible_marker"]
    assert report["complete_assembly_exists"] is True
    assert possible["decision"] is False
    assert possible["reason"] == "no_assembly_contains_marker"
    assert possible["certificate"]["claim"] == "no_complete_assembly_contains_marker"
    guaranteed = report["contracts"]["guaranteed_marker"]
    assert guaranteed["decision"] is False
    assert guaranteed["counterexample"]["contains_marker"] is False


def test_verify_report_accepts_matching_binding(lot01: dict) -> None:
    report = adjudicate_assembly(lot01)
    assert verify_report(lot01, report) == {"ok": True, "problems": []}


def test_verify_report_rejects_snapshot_misbinding(lot01: dict, lot02: dict) -> None:
    report = adjudicate_assembly(lot01)
    result = verify_report(lot02, report)
    assert result["ok"] is False
    assert any("sha256" in problem for problem in result["problems"])


def test_verify_report_rejects_tampered_snapshot(lot01: dict, tmp_path: Path) -> None:
    text = LOT01.read_text(encoding="utf-8")
    tampered_file = tmp_path / "detail-2026-01.html"
    tampered_file.write_text(text.replace('data-count="1"', 'data-count="2"', 1), encoding="utf-8")
    tampered = inspect_barcode_snapshot(tampered_file, root=tmp_path)
    report = adjudicate_assembly(lot01)
    result = verify_report(tampered, report)
    assert result["ok"] is False
    assert any("sha256" in p or "digest" in p for p in result["problems"])


def test_verify_report_rejects_tampered_report(lot01: dict) -> None:
    report = adjudicate_assembly(lot01)
    forged = copy.deepcopy(report)
    forged["contracts"]["guaranteed_marker"]["decision"] = True
    result = verify_report(lot01, forged)
    assert result["ok"] is False
    assert any("guaranteed_marker" in problem for problem in result["problems"])


def test_illegal_fragment_character_in_snapshot_rejected(tmp_path: Path) -> None:
    text = LOT01.read_text(encoding="utf-8")
    bad_file = tmp_path / "detail-2026-01.html"
    bad_file.write_text(text.replace('data-fragment="GA"', 'data-fragment="GX"'), encoding="utf-8")
    with pytest.raises(BarcodeEvidenceError):
        inspect_barcode_snapshot(bad_file, root=tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"fragments": [
            {"fragment": "AX", "count": 1},
            {"fragment": "AG", "count": 1},
            {"fragment": "CA", "count": 1},
            {"fragment": "GA", "count": 1},
        ]},
        {"marker": "C1G"},
        {"marker": "cag"},
        {"start": "B"},
        {"end": "t"},
        {"fragments": [
            {"fragment": "AC", "count": 1},
            {"fragment": "AC", "count": 2},
            {"fragment": "AG", "count": 1},
            {"fragment": "CA", "count": 1},
            {"fragment": "GA", "count": 1},
        ]},
        {"fragments": [
            {"fragment": "AC", "count": 0},
            {"fragment": "AG", "count": 1},
            {"fragment": "CA", "count": 1},
            {"fragment": "GA", "count": 1},
        ]},
    ],
    ids=["bad-fragment-char", "bad-marker-char", "lowercase-marker",
         "bad-start", "lowercase-end", "duplicate-fragment", "zero-count"],
)
def test_illegal_evidence_dicts_rejected(lot01: dict, change: dict) -> None:
    with pytest.raises(BarcodeAssemblyError):
        adjudicate_assembly(_mutated(lot01, **change))


def test_adjudication_is_deterministic_and_pure(lot01: dict) -> None:
    snapshot = copy.deepcopy(lot01)
    first = adjudicate_assembly(lot01)
    second = adjudicate_assembly(lot01)
    assert first == second
    assert lot01 == snapshot
    assert evidence_digest(lot01) == first["evidence_digest"]


def test_cli_single_contract_outputs_receipt(capsys: pytest.CaptureFixture) -> None:
    code = assembly_main([str(LOT01), "--contract", "possible_marker"])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out["contracts"]) == {"possible_marker"}
    assert out["contracts"]["possible_marker"]["witness"]["assembled"] == "ACAGA"
    expected_sha = inspect_barcode_snapshot(LOT01, version="2026-01")["snapshot_sha256"]
    assert out["snapshot_sha256"] == expected_sha


def test_cli_module_invocation_decides_lot03() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "selregress.barcode_assembly", str(LOT03)],
        capture_output=True, text=True, cwd=PROJECT_ROOT, check=False,
    )
    assert proc.returncode == 0
    report = json.loads(proc.stdout)
    assert report["contracts"]["guaranteed_marker"]["decision"] is True
    assert report["contracts"]["possible_marker"]["decision"] is True


def test_cli_verify_roundtrip_and_misbinding(lot01: dict, tmp_path: Path,
                                             capsys: pytest.CaptureFixture) -> None:
    report_file = tmp_path / "report.json"
    report_file.write_text(stable_json_text(adjudicate_assembly(lot01)), encoding="utf-8")
    assert assembly_main([str(LOT01), "--verify", str(report_file)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert assembly_main([str(LOT02), "--verify", str(report_file)]) == 2
    assert json.loads(capsys.readouterr().out)["ok"] is False


def test_cli_rejects_missing_snapshot(capsys: pytest.CaptureFixture) -> None:
    assert assembly_main(["fixtures/barcode/detail-2099-01.html"]) == 3
    assert "error" in json.loads(capsys.readouterr().err)
