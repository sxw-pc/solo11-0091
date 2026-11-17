from __future__ import annotations

from pathlib import Path

import pytest

from selregress.certificate_evidence import CertificateSnapshotError, inspect_certificate_snapshot
from selregress.util import PROJECT_ROOT

FIXTURE = PROJECT_ROOT / "fixtures" / "certificate_registry" / "detail-2026-01.html"


def test_local_registry_snapshot_has_real_split_and_competing_spend() -> None:
    evidence = inspect_certificate_snapshot(FIXTURE)
    assert evidence["certificate"] == "REC-001"
    assert evidence["claim_mwh"] == 6
    assert evidence["competing_spends"] == {"M:0": ["S_A", "S_B"]}
    events = {event["id"]: event for event in evidence["events"]}
    assert [(x["owner"], x["units"]) for x in events["S_A"]["outputs"]] == [("Pool-A", 6), ("Pool-B", 4)]
    assert events["S_B"]["outputs"] == [{"id": "S_B:0", "owner": "Broker", "units": 10}]
    assert events["S_A"]["authorizer"] == events["S_B"]["authorizer"] == "Generator"
    assert events["T_A"]["authorizer"] == "Pool-A"
    assert events["R_A"]["authorizer"] == "Consumer"
    assert events["R_A"]["input"] == "T_A:0"
    assert events["R_A"]["outputs"][0]["owner"] == "retired"
    assert evidence["witnesses"]["S_A"] == ["east", "west"]
    assert evidence["witnesses"]["S_B"] == ["central"]
    assert evidence["strict_no_fork_decision"] is evidence["quorum_witness_decision"] is None
    assert len(evidence["snapshot_sha256"]) == 64


def test_duplicate_output_and_witness_block_snapshot(tmp_path: Path) -> None:
    text = FIXTURE.read_text(encoding="utf-8")
    file = tmp_path / "detail-2026-01.html"
    file.write_text(text.replace('data-output="S_B:0"', 'data-output="S_A:0"'), encoding="utf-8")
    with pytest.raises(CertificateSnapshotError, match="duplicate"):
        inspect_certificate_snapshot(file, root=tmp_path)
    file.write_text(text.replace('data-event="S_B" data-mirror="central"',
                                 'data-event="S_A" data-mirror="east"'), encoding="utf-8")
    with pytest.raises(CertificateSnapshotError, match="duplicate mirror"):
        inspect_certificate_snapshot(file, root=tmp_path)
