from __future__ import annotations

from pathlib import Path

import pytest

from selregress.query_privacy_evidence import QueryEvidenceError, inspect_query_snapshot, preview_query_set
from selregress.util import PROJECT_ROOT

FIXTURE = PROJECT_ROOT / "fixtures" / "query_privacy"


def test_three_individually_safe_queries_leak_when_composed() -> None:
    evidence = inspect_query_snapshot(FIXTURE / "detail-2026-01.html")
    assert [x["utility"] for x in evidence["queries"]] == [4, 4, 7]
    assert all(preview_query_set(evidence, [qid])["all_people_ambiguous"]
               for qid in ["Q1", "Q2", "Q3"])
    pair = preview_query_set(evidence, ["Q1", "Q2"])
    assert pair["consistent_worlds"] == 2
    assert pair["all_people_ambiguous"] is True
    assert pair["utility_total"] == 8
    triple = preview_query_set(evidence, ["Q1", "Q2", "Q3"])
    assert triple["possible_values"] == {"A": [1], "B": [0], "C": [1]}
    assert triple["all_people_ambiguous"] is False
    assert evidence["independent_release_decision"] is evidence["composition_safe_decision"] is None


def test_larger_overlap_graph_remains_real_and_consistent() -> None:
    evidence = inspect_query_snapshot(FIXTURE / "detail-2026-02.html", version="2026-02")
    assert len(evidence["persons"]) == 10
    assert len(evidence["queries"]) == 24
    all_cards = preview_query_set(evidence, [query["id"] for query in evidence["queries"]])
    assert all_cards["consistent_worlds"] >= 1
    assert all_cards["all_people_ambiguous"] is False
    assert all_cards["publishable"] is False


def test_duplicate_card_and_unknown_person_block_html(tmp_path: Path) -> None:
    text = (FIXTURE / "detail-2026-01.html").read_text(encoding="utf-8")
    file = tmp_path / "detail-2026-01.html"
    file.write_text(text.replace('data-query="Q2"', 'data-query="Q1"'), encoding="utf-8")
    with pytest.raises(QueryEvidenceError, match="duplicate"):
        inspect_query_snapshot(file, root=tmp_path)
    file.write_text(text.replace('data-members="B C"', 'data-members="B X"'), encoding="utf-8")
    with pytest.raises(QueryEvidenceError, match="invalid"):
        inspect_query_snapshot(file, root=tmp_path)
