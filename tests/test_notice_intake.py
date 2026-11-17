from __future__ import annotations

import json
from pathlib import Path

from selregress.cli import main
from selregress.notice_intake import intake_notices


ROOT = Path(__file__).resolve().parents[1]


def test_ready_list_and_details_have_stable_evidence():
    first = intake_notices("fixtures/notice_intake/manifest-ready.json", root=ROOT)
    second = intake_notices("fixtures/notice_intake/manifest-ready.json", root=ROOT)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["status"] == "ready"
    assert [item["notice_id"] for item in first["notices"]] == ["N-001", "N-002"]
    assert first["notices"][0]["detail"]["deadline"] == "2025-06-10"
    assert len(first["notices"][0]["detail"]["sha256"]) == 64


def test_budget_conflict_blocks_intake_without_replacing_source():
    result = intake_notices("fixtures/notice_intake/manifest-conflict.json", root=ROOT)
    assert result["status"] == "blocked"
    assert result["notices"][0]["status"] == "coherent"
    assert result["notices"][1]["status"] == "mismatch"
    assert result["notices"][1]["reasons"] == ["budget_disagrees"]
    assert result["notices"][1]["list"]["budget"] == "2000"
    assert result["notices"][1]["detail"]["budget"] == "2100"


def test_notice_intake_cli_status_tracks_real_mismatch(capsys):
    assert main(["notice-intake", "--manifest", "fixtures/notice_intake/manifest-ready.json"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["status"] == "ready"
    assert main(["notice-intake", "--manifest", "fixtures/notice_intake/manifest-conflict.json"]) == 2
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["notices"][1]["reasons"] == ["budget_disagrees"]
