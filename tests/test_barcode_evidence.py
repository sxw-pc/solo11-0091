from __future__ import annotations

from pathlib import Path

import pytest

from selregress.barcode_evidence import BarcodeEvidenceError, inspect_barcode_snapshot, preview_candidate_assembly
from selregress.util import PROJECT_ROOT

FIXTURE = PROJECT_ROOT / "fixtures" / "barcode"


def test_same_four_fragments_have_opposite_marker_witnesses() -> None:
    evidence = inspect_barcode_snapshot(FIXTURE / "detail-2026-01.html")
    present = preview_candidate_assembly(evidence, "ACAGA")
    absent = preview_candidate_assembly(evidence, "AGACA")
    assert present["valid"] and absent["valid"]
    assert present["contains_marker"] is True
    assert absent["contains_marker"] is False
    assert evidence["required_length"] == 5
    assert evidence["possible_marker_decision"] is evidence["guaranteed_marker_decision"] is None
    assert len(evidence["snapshot_sha256"]) == 64


def test_repeated_fragment_multiplicity_preserves_two_large_witnesses() -> None:
    evidence = inspect_barcode_snapshot(FIXTURE / "detail-2026-02.html", version="2026-02")
    assert evidence["required_length"] == 19
    assert sum(row["count"] for row in evidence["fragments"]) == 18
    present = preview_candidate_assembly(evidence, "A" + "CAGATA" * 3)
    absent = preview_candidate_assembly(evidence, "A" + "GACATA" * 3)
    assert present["valid"] and present["contains_marker"] is True
    assert absent["valid"] and absent["contains_marker"] is False
    assert preview_candidate_assembly(evidence, "ACAGA")["valid"] is False


def test_duplicate_fragment_row_is_rejected(tmp_path: Path) -> None:
    text = (FIXTURE / "detail-2026-01.html").read_text(encoding="utf-8")
    file = tmp_path / "detail-2026-01.html"
    file.write_text(text.replace('data-fragment="CA"', 'data-fragment="AC"'), encoding="utf-8")
    with pytest.raises(BarcodeEvidenceError, match="duplicate"):
        inspect_barcode_snapshot(file, root=tmp_path)


def test_forced_marker_fixture_has_valid_positive_witness() -> None:
    evidence = inspect_barcode_snapshot(FIXTURE / "detail-2026-03.html", version="2026-03")
    candidate = preview_candidate_assembly(evidence, "C" + "AGCTC" * 2)
    assert evidence["required_length"] == 11
    assert candidate["valid"] is True
    assert candidate["contains_marker"] is True
    assert evidence["guaranteed_marker_decision"] is None
