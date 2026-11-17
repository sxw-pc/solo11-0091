from __future__ import annotations

import json
from pathlib import Path

import pytest

from selregress.cli import main
from selregress.notice_intake import plan_review_actions
from selregress.util import InputError, stable_json_text


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = "fixtures/notice_review/manifest.json"
ROSTER = "fixtures/notice_review/roster.json"


def _plan(manifest: str = MANIFEST, roster: str = ROSTER, contract: str = "delegation_allowed"):
    return plan_review_actions(manifest, roster, contract=contract, root=ROOT)


def _notice(result: dict, notice_id: str) -> dict:
    return next(item for item in result["notices"] if item["notice_id"] == notice_id)


def _edge(notice: dict, reviewer: str) -> dict:
    return next(item for item in notice["edges"] if item["reviewer"] == reviewer)


def _write_roster(workspace: Path, mutate) -> str:
    roster = json.loads((ROOT / ROSTER).read_text(encoding="utf-8"))
    mutate(roster)
    target = workspace / "roster.json"
    target.write_text(json.dumps(roster, ensure_ascii=False), encoding="utf-8")
    return str(target)


def test_two_coherent_notices_do_not_imply_two_actions_under_designated_only():
    result = _plan(contract="designated_only")
    assert result["status"] == "blocked"
    assert result["summary"]["coherent_count"] == 2
    assert result["summary"]["actionable_count"] == 1
    first = _notice(result, "N-001")
    assert first["action"] == {"reviewer": "X", "seat_date": "2025-06-09", "via": "designated"}
    second = _notice(result, "N-002")
    assert second["action"] is None
    assert second["reasons"] == ["seat_contended"]
    # 原指定人合同下 Y 的边被合同拒绝，不能凭技能或预算推断可转派。
    y_edge = _edge(second, "Y")
    assert y_edge["authorization"] == "none"
    assert "not_designated" in y_edge["reasons"]
    assert y_edge["feasible"] is False


def test_valid_delegation_contract_completes_both_notices_with_stable_witness():
    result = _plan(contract="delegation_allowed")
    assert result["status"] == "ready"
    assert result["summary"]["actionable_count"] == 2
    assert result["summary"]["delegation_count"] == 1
    # 逐条贪心把 N-001 给 X 会让 N-002 失去唯一合格席位；联合规划必须转出 N-001。
    assert _notice(result, "N-001")["action"] == {
        "reviewer": "Y", "seat_date": "2025-06-09", "via": "delegation"}
    assert _notice(result, "N-002")["action"] == {
        "reviewer": "X", "seat_date": "2025-06-09", "via": "designated"}
    assert result["seat_usage"] == [
        {"reviewer": "X", "date": "2025-06-09", "capacity": 1, "used": 1, "occupied_by": ["N-002"]},
        {"reviewer": "Y", "date": "2025-06-09", "capacity": 1, "used": 1, "occupied_by": ["N-001"]},
    ]
    delegation = _notice(result, "N-001")["delegations"][0]
    assert delegation["valid"] is True
    assert delegation["detail_sha256"] == _notice(result, "N-001")["detail"]["sha256"]


def test_budget_raise_beyond_delegate_authority_drops_back_to_one_action():
    result = _plan(manifest="fixtures/notice_review/manifest-budget-1600.json")
    assert result["summary"]["coherent_count"] == 2
    assert result["summary"]["actionable_count"] == 1
    first = _notice(result, "N-001")
    assert first["budget"] == "1600"
    assert first["action"]["reviewer"] == "X"
    assert first["action"]["via"] == "designated"
    assert "budget_over_authority" in _edge(first, "Y")["reasons"]
    assert _notice(result, "N-002")["reasons"] == ["seat_contended"]


def test_revised_detail_sha_stales_delegation_without_touching_values():
    result = _plan(manifest="fixtures/notice_review/manifest-revised-detail.json")
    assert result["summary"]["coherent_count"] == 2
    assert result["summary"]["actionable_count"] == 1
    first = _notice(result, "N-001")
    assert first["budget"] == "1280"
    assert first["delegations"][0]["valid"] is False
    assert first["delegations"][0]["reasons"] == ["delegation_stale_sha"]
    assert _edge(first, "Y")["reasons"] == ["delegation_stale_sha"]
    assert first["action"] == {"reviewer": "X", "seat_date": "2025-06-09", "via": "designated"}
    assert _notice(result, "N-002")["reasons"] == ["seat_contended"]


def test_stale_delegation_becomes_notice_level_reason_when_designee_cannot_serve(workspace):
    roster = _write_roster(workspace, lambda data: data["reviewers"][0].update(
        seats=[{"date": "2025-06-10", "count": 1}]))
    result = _plan(manifest="fixtures/notice_review/manifest-revised-detail.json", roster=roster)
    first = _notice(result, "N-001")
    assert first["action"] is None
    assert first["reasons"] == ["delegation_stale_sha", "seat_expired"]
    # N-002 截止更晚，X 的 06-10 席位仍有效，坏公告不抹掉其他公告的证据。
    assert _notice(result, "N-002")["action"]["reviewer"] == "X"
    assert result["summary"]["actionable_count"] == 1


def test_skill_change_recomputes_actions(workspace):
    def mutate(data):
        data["reviewers"][1]["skills"] = ["legal"]

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    first = _notice(result, "N-001")
    assert "skill_missing" in _edge(first, "Y")["reasons"]
    assert first["action"]["reviewer"] == "X"
    assert result["summary"]["actionable_count"] == 1


def test_notice_level_skill_missing_when_no_authorized_reviewer_matches(workspace):
    def mutate(data):
        data["reviewers"][0]["skills"] = ["office"]

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    second = _notice(result, "N-002")
    assert second["action"] is None
    assert second["reasons"] == ["skill_missing"]
    assert _notice(result, "N-001")["action"] is not None


def test_notice_level_budget_overflow_when_all_authorized_reviewers_exceeded(workspace):
    def mutate(data):
        data["reviewers"][0]["budget_limit"] = "1000"

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    second = _notice(result, "N-002")
    assert second["action"] is None
    assert second["reasons"] == ["budget_over_authority"]
    # N-001 预算 1280 超出 X 的 1000，但有效委托让 Y 接手。
    first = _notice(result, "N-001")
    assert first["action"] == {"reviewer": "Y", "seat_date": "2025-06-09", "via": "delegation"}


def test_seat_removal_recomputes_actions(workspace):
    def mutate(data):
        data["reviewers"][1]["seats"] = []

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    first = _notice(result, "N-001")
    assert "seat_unavailable" in _edge(first, "Y")["reasons"]
    assert first["action"]["reviewer"] == "X"
    assert result["summary"]["actionable_count"] == 1


def test_expired_seat_fixture_blocks_delegation_edge():
    result = _plan(roster="fixtures/notice_review/roster-expired-seat.json")
    first = _notice(result, "N-001")
    assert _edge(first, "Y")["authorization"] == "delegation"
    assert _edge(first, "Y")["reasons"] == ["seat_expired"]
    assert first["action"]["reviewer"] == "X"
    assert result["summary"]["actionable_count"] == 1


def test_notice_level_seat_expired_when_all_seats_fall_on_or_after_deadline(workspace):
    def mutate(data):
        for reviewer in data["reviewers"]:
            reviewer["seats"] = [{"date": "2025-06-10", "count": 1}]

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    first = _notice(result, "N-001")
    assert first["action"] is None
    assert first["reasons"] == ["seat_expired"]
    # N-002 截止 06-12，06-10 席位仍在截止前。
    assert _notice(result, "N-002")["action"]["seat_date"] == "2025-06-10"


def test_delegation_is_never_inferred_without_explicit_sha_bound_record(workspace):
    def mutate(data):
        data["delegations"] = []

    roster = _write_roster(workspace, mutate)
    result = _plan(roster=roster)
    first = _notice(result, "N-001")
    # Y 技能与预算都满足，但没有显式委托，合同不允许推断可转派。
    assert _edge(first, "Y")["authorization"] == "none"
    assert _edge(first, "Y")["reasons"] == ["delegation_absent"]
    assert first["action"]["reviewer"] == "X"
    assert result["summary"]["actionable_count"] == 1


def test_mismatch_notice_stays_out_of_allocation_but_keeps_evidence():
    result = _plan(manifest="fixtures/notice_review/manifest-mismatch.json")
    assert result["status"] == "blocked"
    second = _notice(result, "N-002")
    assert second["intake_status"] == "mismatch"
    assert second["intake_reasons"] == ["budget_disagrees"]
    assert second["reasons"] == ["mismatch"]
    assert second["action"] is None
    assert second["edges"] == []
    # 来源路径、预算/截止、详情SHA等证据保留。
    assert second["list"]["path"] == "fixtures/notice_review/list.html"
    assert second["list"]["budget"] == "2000"
    assert second["detail"]["budget"] == "2100"
    assert second["detail"]["deadline"] == "2025-06-15"
    assert len(second["detail"]["sha256"]) == 64
    occupied = [item for seat in result["seat_usage"] for item in seat["occupied_by"]]
    assert occupied == ["N-001"]
    # X 的席位空出后，规划选择最小化转派：N-001 回到原指定人。
    first = _notice(result, "N-001")
    assert first["action"] == {"reviewer": "X", "seat_date": "2025-06-09", "via": "designated"}
    assert result["summary"]["actionable_count"] == 1


def test_missing_detail_blocks_only_that_notice(workspace):
    manifest = json.loads((ROOT / MANIFEST).read_text(encoding="utf-8"))
    manifest["details"] = [item for item in manifest["details"] if item["notice_id"] != "N-002"]
    target = workspace / "manifest.json"
    target.write_text(json.dumps(manifest), encoding="utf-8")
    result = _plan(manifest=str(target))
    second = _notice(result, "N-002")
    assert second["intake_status"] == "missing_detail"
    assert second["reasons"] == ["missing_detail"]
    assert second["detail"] is None
    assert _notice(result, "N-001")["action"] is not None
    assert result["summary"]["actionable_count"] == 1


def test_notice_without_roster_entry_is_flagged_not_silently_actionable(workspace):
    roster = _write_roster(workspace, lambda data: data["notices"].pop(1))
    result = _plan(roster=roster)
    second = _notice(result, "N-002")
    assert second["reasons"] == ["roster_entry_missing"]
    assert second["action"] is None
    assert result["status"] == "blocked"


def test_same_input_repeated_runs_are_byte_stable():
    for contract in ("designated_only", "delegation_allowed"):
        first = stable_json_text(_plan(contract=contract))
        second = stable_json_text(_plan(contract=contract))
        assert first == second


def test_cli_exit_codes_track_both_contracts(capsys):
    base = ["notice-review", "--manifest", MANIFEST, "--roster", ROSTER]
    assert main([*base, "--contract", "designated_only"]) == 2
    designated = json.loads(capsys.readouterr().out)
    assert designated["status"] == "blocked"
    assert designated["summary"]["actionable_count"] == 1
    assert main([*base, "--contract", "delegation_allowed"]) == 0
    delegated = json.loads(capsys.readouterr().out)
    assert delegated["status"] == "ready"
    assert delegated["summary"]["actionable_count"] == 2
    assert main([*base, "--contract", "delegation_allowed"]) == 0
    assert json.loads(capsys.readouterr().out) == delegated


def test_cli_rejects_unknown_contract(capsys):
    code = main(["notice-review", "--manifest", MANIFEST, "--roster", ROSTER, "--contract", "open"])
    assert code == 3
    assert json.loads(capsys.readouterr().err)["error"] == "CLI_ARGUMENT"


def test_unknown_contract_value_is_an_input_error():
    with pytest.raises(InputError) as caught:
        _plan(contract="open")
    assert caught.value.code == "REVIEW_CONTRACT_UNKNOWN"


def test_roster_rejects_delegation_from_non_designee(workspace):
    def mutate(data):
        data["reviewers"].append({"reviewer_id": "Z", "skills": ["office"], "budget_limit": "5000",
                                  "seats": [{"date": "2025-06-09", "count": 1}]})
        data["delegations"][0]["from"] = "Z"

    roster = _write_roster(workspace, mutate)
    with pytest.raises(InputError) as caught:
        _plan(roster=roster)
    assert caught.value.code == "REVIEW_ROSTER_INVALID"


def test_roster_rejects_malformed_delegation_sha(workspace):
    def mutate(data):
        data["delegations"][0]["detail_sha256"] = "not-a-sha"

    roster = _write_roster(workspace, mutate)
    with pytest.raises(InputError) as caught:
        _plan(roster=roster)
    assert caught.value.code == "REVIEW_ROSTER_INVALID"


def test_roster_rejects_duplicate_reviewer(workspace):
    def mutate(data):
        data["reviewers"].append(dict(data["reviewers"][0]))

    roster = _write_roster(workspace, mutate)
    with pytest.raises(InputError) as caught:
        _plan(roster=roster)
    assert caught.value.code == "REVIEW_ROSTER_INVALID"


def test_missing_roster_file_is_an_input_error():
    with pytest.raises(InputError) as caught:
        _plan(roster="fixtures/notice_review/roster-absent.json")
    assert caught.value.code == "REVIEW_ROSTER_MISSING"
