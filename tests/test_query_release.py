from __future__ import annotations

from itertools import combinations, product
from pathlib import Path

import pytest

from selregress.query_privacy_evidence import inspect_query_snapshot
from selregress.query_release import (
    CONTRACT_COMPOSITION,
    CONTRACT_INDEPENDENT,
    CONTRACTS,
    PUBLIC_KIND,
    RECEIPT_KIND,
    QueryReleaseError,
    decide_query_release,
    public_release_artifact,
    verify_query_receipt,
)
from selregress.util import PROJECT_ROOT, canonical_digest

FIXTURE = PROJECT_ROOT / "fixtures" / "query_privacy"


def evidence(name: str):
    version = name.split("-")[-1].removesuffix(".html")
    path = FIXTURE / name
    return inspect_query_snapshot(path, version=version)


def world_set(item, selected: list[str]) -> list[dict[str, int]]:
    """Independent brute-force oracle for tests (no release-module reuse)."""
    people = item["persons"]
    cards = {query["id"]: query for query in item["queries"]}
    worlds = []
    for assignment in product((0, 1), repeat=len(people)):
        values = dict(zip(people, assignment))
        if all(sum(values[person] for person in cards[query_id]["members"])
               == cards[query_id]["positive_count"] for query_id in selected):
            worlds.append(values)
    return worlds


def everyone_has_two_values(worlds, people) -> bool:
    return bool(worlds) and all({world[person] for world in worlds} == {0, 1}
                                for person in people)


def best_safe_set(item) -> tuple[int, tuple[str, ...]]:
    """Independent oracle: exhaustive search over consistent-world masks.

    For small card sets every subset is enumerated. For larger sets the oracle
    folds all cards into a map from intersection-world-mask to the best
    utility and ID tuple seen, which still visits every distinct intersection
    without reusing the release module's branch-and-bound.
    """
    people = item["persons"]
    cards = {query["id"]: query for query in item["queries"]}
    if len(cards) <= 6:
        options = []
        for size in range(1, len(cards) + 1):
            for ids in combinations(sorted(cards), size):
                if not everyone_has_two_values(
                        world_set(item, list(ids)), people):
                    continue
                options.append((sum(cards[query_id]["utility"] for query_id in ids), ids))
        options.sort(key=lambda entry: (-entry[0], len(entry[1]), entry[1]))
        return options[0]
    full = (1 << (1 << len(people))) - 1
    states: dict[int, tuple[int, tuple[str, ...]]] = {full: (0, ())}
    for query in item["queries"]:
        mask = 0
        members = [people.index(person) for person in query["members"]]
        for world in range(1 << len(people)):
            if sum(world >> position & 1 for position in members) == query["positive_count"]:
                mask |= 1 << world
        updates: dict[int, tuple[int, tuple[str, ...]]] = {}
        for current, (utility, ids) in states.items():
            narrowed = current & mask
            if not narrowed:
                continue
            candidate = (utility + query["utility"], tuple(sorted(ids + (query["id"],))))
            existing = updates.get(narrowed) or states.get(narrowed)
            if existing is None or (-candidate[0], len(candidate[1]), candidate[1]) < (
                    -existing[0], len(existing[1]), existing[1]):
                updates[narrowed] = candidate
        for narrowed, candidate in updates.items():
            existing = states.get(narrowed)
            if existing is None or (-candidate[0], len(candidate[1]), candidate[1]) < (
                    -existing[0], len(existing[1]), existing[1]):
                states[narrowed] = candidate
    mask_index = {
        sum(value << position for position, value in enumerate(assignment)): assignment
        for assignment in product((0, 1), repeat=len(people))
    }

    def mask_is_safe(mask: int) -> bool:
        if not mask:
            return False
        worlds = [dict(zip(people, mask_index[world]))
                  for world in mask_index if mask >> world & 1]
        return everyone_has_two_values(worlds, people)

    options = [
        (utility, ids) for mask, (utility, ids) in states.items()
        if mask_is_safe(mask)
    ]
    options.sort(key=lambda entry: (-entry[0], len(entry[1]), entry[1]))
    return options[0]


def test_same_release_needs_opposite_answers_per_contract() -> None:
    item = evidence("detail-2026-01.html")
    independent = decide_query_release(item, CONTRACT_INDEPENDENT)
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert independent["approved_query_ids"] == ["Q1", "Q2", "Q3"]
    assert composition["approved_query_ids"] == ["Q1", "Q2"]
    assert composition["withheld_query_ids"] == ["Q3"]
    assert composition["approved_utility_total"] == 8
    assert independent["contract"] != composition["contract"]
    for receipt in (independent, composition):
        assert receipt["internal_only"] is True
        assert receipt["publishable"] is False
        assert receipt["snapshot_sha256"] == item["snapshot_sha256"]
        assert receipt["kind"] == RECEIPT_KIND


def test_composition_never_claims_pairwise_safety() -> None:
    item = evidence("detail-2026-01.html")
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["cross_card_safety_claimed"] is True
    independent = decide_query_release(item, CONTRACT_INDEPENDENT)
    assert independent["cross_card_safety_claimed"] is False
    # Q1 and Q2 are individually safe, but the module never derives a joint
    # claim from per-card safety; it recomputes the intersection worlds.
    assert composition["consistent_worlds"] == 2
    assert everyone_has_two_values(
        world_set(item, composition["approved_query_ids"]), item["persons"])


def test_zero_one_worlds_are_checked_not_real_rank() -> None:
    # P1/P2/P3 are pairwise sums equal to 1. Over the reals the three
    # equations have a solution (A=B=C=0.5), but no 0/1 world exists. The
    # composition contract must withhold P3 rather than trust the real rank.
    item = evidence("detail-2026-03.html")
    assert all(len(world_set(item, [query_id])) == 4
               for query_id in ("P1", "P2", "P3"))
    assert world_set(item, ["P1", "P2", "P3"]) == []
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["P1", "P2"]
    assert composition["approved_utility_total"] == best_safe_set(item)[0]
    assert composition["withheld_query_ids"] == ["P3"]
    assert len(world_set(item, composition["approved_query_ids"])) == 2


def test_people_individually_ambiguous_but_no_global_complementary_world_pair() -> None:
    item = evidence("detail-2026-09.html")
    all_ids = [query["id"] for query in item["queries"]]
    worlds = world_set(item, all_ids)
    # A is fixed at 0, but B, C and D each take both values somewhere.
    assert {world["A"] for world in worlds} == {0}
    assert all({world[person] for world in worlds} == {0, 1} for person in "BCD")
    # Even the B/C/D worlds have no global complement among consistent worlds,
    # so per-person "two possible values" must not be misread as a
    # complementary world pair for everybody.
    assert not any(tuple(1 - world[person] for person in item["persons"]) in
                   [tuple(other[person] for person in item["persons"]) for other in worlds]
                   for world in worlds)
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["G1", "G2"]
    assert composition["approved_utility_total"] == 8


def test_inconsistent_full_set_is_pruned_from_the_release() -> None:
    item = evidence("detail-2026-07.html")
    assert world_set(item, ["C1", "C2", "C3"]) == []
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["C1", "C3"]
    assert composition["consistent_worlds"] == 2
    assert everyone_has_two_values(
        world_set(item, composition["approved_query_ids"]), item["persons"])


def test_no_card_safe_falls_back_to_the_empty_release() -> None:
    # E1 and E3 both pin every member at once (counts 3 and 0), so no single
    # card leaves anyone two-valued.
    item = {
        "persons": ["A", "B", "C"],
        "queries": [
            {"id": "E1", "members": ["A", "B", "C"], "positive_count": 3,
             "utility": 4},
            {"id": "E2", "members": ["A", "B", "C"], "positive_count": 0,
             "utility": 4},
            {"id": "E3", "members": ["A", "B", "C"], "positive_count": 3,
             "utility": 3},
        ],
    }
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == []
    assert composition["withheld_query_ids"] == ["E1", "E2", "E3"]
    assert composition["approved_utility_total"] == 0
    assert composition["consistent_worlds"] == 8
    assert [witness["person"] for witness in composition["joint_witnesses"]] == [
        "A", "B", "C"]
    public = public_release_artifact(composition)
    assert public["queries"] == []
    verify_query_receipt(composition, item)


def test_unsafe_pair_never_merges_from_two_safe_cards() -> None:
    item = evidence("detail-2026-06.html")
    per_card = decide_query_release(item, CONTRACT_INDEPENDENT)
    assert per_card["approved_query_ids"] == ["L1", "L2"]
    # L1 and L2 are individually safe, but their intersection forces C=0,
    # so releasing both would expose C. L1+L3 and L2+L3 are inconsistent.
    pair_worlds = world_set(item, ["L1", "L2"])
    assert {world["C"] for world in pair_worlds} == {0}
    assert {world["A"] for world in pair_worlds} == {0, 1}
    assert {world["B"] for world in pair_worlds} == {0, 1}
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["L1"]
    assert composition["approved_utility_total"] == 9


def test_utility_tie_breaks_on_fewest_then_lexicographic_ids() -> None:
    item = evidence("detail-2026-04.html")
    all_options = []
    for size in (1, 2, 3, 4):
        for ids in combinations(["T1", "T2", "T3", "T4"], size):
            if everyone_has_two_values(world_set(item, list(ids)), item["persons"]):
                all_options.append((sum(5 for _ in ids), ids))
    maximum = max(utility for utility, _ in all_options)
    tied = sorted(option for utility, option in all_options if utility == maximum)
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_utility_total"] == maximum
    assert tuple(composition["approved_query_ids"]) == tied[0]
    assert composition["optimality_certificate"]["optimal_utility"] == maximum


def test_zero_and_full_positive_counts() -> None:
    item = evidence("detail-2026-05.html")
    assert world_set(item, ["Z1"]) == [
        {"A": 0, "B": 0, "C": 0, "D": 0},
        {"A": 0, "B": 0, "C": 0, "D": 1},
        {"A": 0, "B": 0, "C": 1, "D": 0},
        {"A": 0, "B": 0, "C": 1, "D": 1},
    ]
    assert len(world_set(item, ["Z2"])) == 4
    independent = decide_query_release(item, CONTRACT_INDEPENDENT)
    # Z1/Z2 pin both members of {A, B}, so they expose A and B alone.
    assert independent["approved_query_ids"] == ["Z3"]
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["Z3"]
    assert composition["approved_utility_total"] == 5
    assert composition["consistent_worlds"] == 8


def test_person_left_always_unknown_limits_nothing() -> None:
    item = evidence("detail-2026-08.html")
    assert not any("D" in query["members"] for query in item["queries"])
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    assert composition["approved_query_ids"] == ["U1", "U2"]
    assert composition["approved_utility_total"] == 10
    # D stays fully free in every consistent world because no card mentions D.
    assert all(world["D"] in {0, 1} for world in
               world_set(item, composition["approved_query_ids"]))
    assert everyone_has_two_values(
        world_set(item, composition["approved_query_ids"]), item["persons"])


def test_24_card_global_optimality_is_proven_not_greedy() -> None:
    item = evidence("detail-2026-02.html")
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    independent = decide_query_release(item, CONTRACT_INDEPENDENT)
    # Every card is individually safe, so the independent contract releases all
    # of them; that must not be mistaken for a composition-safe release.
    assert len(independent["approved_query_ids"]) == 24
    assert independent["approved_utility_total"] == 131
    assert not everyone_has_two_values(
        world_set(item, independent["approved_query_ids"]), item["persons"])
    best_utility, best_ids = best_safe_set(item)
    assert composition["approved_utility_total"] == best_utility == 49
    assert tuple(composition["approved_query_ids"]) == best_ids
    certificate = composition["optimality_certificate"]
    assert certificate["method"] == "exhaustive_branch_and_bound"
    assert certificate["search_complete"] is True
    assert certificate["world_universe_size"] == 1024
    assert certificate["query_count"] == 24
    assert certificate["optimal_utility"] == best_utility
    assert certificate["utility_upper_bound"] == 131
    assert certificate["explored_nodes"] > 0
    assert everyone_has_two_values(
        world_set(item, composition["approved_query_ids"]), item["persons"])


def test_receipts_are_bound_to_contract_and_snapshot() -> None:
    item = evidence("detail-2026-01.html")
    other = evidence("detail-2026-02.html")
    receipt = decide_query_release(item, CONTRACT_COMPOSITION)
    verify_query_receipt(receipt, item, expected_contract=CONTRACT_COMPOSITION)
    with pytest.raises(QueryReleaseError, match="different contract"):
        verify_query_receipt(receipt, item, expected_contract=CONTRACT_INDEPENDENT)
    with pytest.raises(QueryReleaseError, match="does not match evidence"):
        verify_query_receipt(receipt, other, expected_contract=CONTRACT_COMPOSITION)
    tampered = dict(receipt, approved_utility_total=99)
    with pytest.raises(QueryReleaseError, match="digest mismatch"):
        verify_query_receipt(tampered, item)


def test_contract_receipts_cannot_be_reused_across_contracts() -> None:
    item = evidence("detail-2026-02.html")
    composition = decide_query_release(item, CONTRACT_COMPOSITION)
    swapped = dict(composition, contract=CONTRACT_INDEPENDENT)
    with pytest.raises(QueryReleaseError, match="digest mismatch"):
        public_release_artifact(swapped)
    # A forged but internally re-signed receipt (here: an impossible decision
    # under the composition contract) is caught by re-running the contract.
    forged = dict(composition, approved_query_ids=["X00"],
                  withheld_query_ids=[query["id"] for query in item["queries"]
                                      if query["id"] != "X00"])
    forged["approved_utility_total"] = 4
    forged["decision_sha256"] = canonical_digest(
        {key: value for key, value in forged.items() if key != "decision_sha256"})
    with pytest.raises(QueryReleaseError, match="not reproducible"):
        verify_query_receipt(forged, item, expected_contract=CONTRACT_COMPOSITION)
    # Receipts issued for one contract cannot be replayed under the other.
    with pytest.raises(QueryReleaseError, match="different contract"):
        verify_query_receipt(composition, item, expected_contract=CONTRACT_INDEPENDENT)


def test_public_artifact_drops_withheld_counts_and_witnesses() -> None:
    item = evidence("detail-2026-01.html")
    receipt = decide_query_release(item, CONTRACT_COMPOSITION)
    public = public_release_artifact(receipt, expected_contract=CONTRACT_COMPOSITION)
    assert public["kind"] == PUBLIC_KIND
    assert public["schema_version"] == 1
    assert public == {
        "kind": PUBLIC_KIND,
        "schema_version": 1,
        "queries": [
            {"id": "Q1", "members": ["A", "B"], "positive_count": 1},
            {"id": "Q2", "members": ["B", "C"], "positive_count": 1},
        ],
    }
    text = repr(public)
    assert "Q3" not in text
    assert item["snapshot_sha256"] not in text
    assert receipt["decision_sha256"] not in text
    for banned in ("world_0", "world_1", "joint_witnesses", "withheld", "utility"):
        assert banned not in text
    # The internal receipt still contains those internals, marked internal.
    assert receipt["publishable"] is False
    assert len(receipt["joint_witnesses"]) == len(item["persons"])


def test_public_artifact_rejects_foreign_contract() -> None:
    item = evidence("detail-2026-02.html")
    receipt = decide_query_release(item, CONTRACT_COMPOSITION)
    with pytest.raises(QueryReleaseError, match="different contract"):
        public_release_artifact(receipt, expected_contract=CONTRACT_INDEPENDENT)
    receipt["publishable"] = True
    with pytest.raises(QueryReleaseError, match="internal and unpublished"):
        public_release_artifact(receipt)


def test_receipts_do_not_leak_into_preview_output() -> None:
    item = evidence("detail-2026-01.html")
    assert item["independent_release_decision"] is None
    assert item["composition_safe_decision"] is None
    receipt = decide_query_release(item, CONTRACT_COMPOSITION)
    # Deciding is a pure function of evidence; the diagnostic evidence stays
    # untouched and keeps both decision slots null.
    assert item["independent_release_decision"] is None
    assert item["composition_safe_decision"] is None
    assert receipt["snapshot_path"] == item["snapshot_path"]


def test_invalid_contract_unknown_person_and_duplicate_card_are_blocked() -> None:
    item = evidence("detail-2026-01.html")
    with pytest.raises(QueryReleaseError, match="unknown release contract"):
        decide_query_release(item, "release_everything")
    duplicate = {
        **item,
        "queries": [*item["queries"][:-1],
                    {**item["queries"][-1], "id": "Q1"}],
    }
    with pytest.raises(QueryReleaseError, match="duplicate or invalid"):
        decide_query_release(duplicate, CONTRACT_COMPOSITION)
    stranger = {
        **item,
        "queries": [*item["queries"][:-1],
                    {**item["queries"][-1], "members": ["A", "X"]}],
    }
    with pytest.raises(QueryReleaseError, match="duplicate or invalid"):
        decide_query_release(stranger, CONTRACT_COMPOSITION)
    no_queries = {**item, "queries": item["queries"][:2]}
    with pytest.raises(QueryReleaseError, match="too few"):
        decide_query_release(no_queries, CONTRACT_COMPOSITION)


def test_snapshot_correction_changes_receipt_and_rejects_stale_publication() -> None:
    original = evidence("detail-2026-01.html")
    corrected_evidence = {
        **original,
        "queries": [
            {**original["queries"][0], "positive_count": 2},
            original["queries"][1],
            original["queries"][2],
        ],
    }
    corrected = decide_query_release(corrected_evidence, CONTRACT_COMPOSITION)
    # Q1 corrected to "both A and B positive" makes the Q1+Q2 and Q1+Q3
    # pairs uniquely identify their members; only Q3 alone remains safe.
    assert corrected["approved_query_ids"] == ["Q3"]
    assert corrected["approved_utility_total"] == 7
    assert corrected["decision_sha256"] != decide_query_release(
        original, CONTRACT_COMPOSITION)["decision_sha256"]
    with pytest.raises(QueryReleaseError, match="not reproducible"):
        verify_query_receipt(corrected, original, expected_contract=CONTRACT_COMPOSITION)
    with pytest.raises(QueryReleaseError, match="not reproducible"):
        verify_query_receipt(
            decide_query_release(original, CONTRACT_COMPOSITION),
            corrected_evidence, expected_contract=CONTRACT_COMPOSITION)


def test_all_fixtures_keep_public_output_free_of_internal_material() -> None:
    for path in sorted(FIXTURE.glob("detail-2026-0*.html")):
        version = path.name.split("-")[-1].removesuffix(".html")
        item = inspect_query_snapshot(path, version=version)
        for contract in CONTRACTS:
            receipt = decide_query_release(item, contract)
            verify_query_receipt(receipt, item, expected_contract=contract)
            public = public_release_artifact(receipt, expected_contract=contract)
            approved = set(receipt["approved_query_ids"])
            assert {query["id"] for query in public["queries"]} == approved
            assert all(set(query) == {"id", "members", "positive_count"}
                       for query in public["queries"])
            text = repr(public)
            assert item["snapshot_sha256"] not in text
            assert "world_" not in text
            assert "decision_sha256" not in text
            assert all(query_id not in text for query_id in receipt["withheld_query_ids"])


def test_cli_emits_internal_receipt_and_public_cards(workspace) -> None:
    import json
    import subprocess
    import sys

    public_path = workspace / "public.json"
    receipt_path = workspace / "receipt.json"
    result = subprocess.run(
        [sys.executable, "-m", "selregress", "query-release",
         "--snapshot", "fixtures/query_privacy/detail-2026-01.html",
         "--contract", "composition_safe",
         "--out", str(receipt_path), "--public-out", str(public_path)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "query-release"
    assert payload["internal_receipt"]["approved_query_ids"] == ["Q1", "Q2"]
    assert payload["public_release"]["queries"] == [
        {"id": "Q1", "members": ["A", "B"], "positive_count": 1},
        {"id": "Q2", "members": ["B", "C"], "positive_count": 1},
    ]
    written = json.loads(public_path.read_text(encoding="utf-8"))
    assert written == payload["public_release"]
    saved_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    verify_query_receipt(saved_receipt, evidence("detail-2026-01.html"),
                         expected_contract=CONTRACT_COMPOSITION)


def test_cli_rejects_paths_outside_root_and_unknown_files(workspace) -> None:
    import subprocess
    import sys

    outside = subprocess.run(
        [sys.executable, "-m", "selregress", "query-release",
         "--snapshot", "fixtures/query_privacy/detail-2026-01.html",
         "--contract", "composition_safe", "--out", "/tmp/escape.json"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert outside.returncode == 3
    assert "PATH_OUTSIDE_ROOT" in outside.stderr
    missing = subprocess.run(
        [sys.executable, "-m", "selregress", "query-release",
         "--snapshot", "fixtures/query_privacy/missing-2026-01.html",
         "--contract", "composition_safe"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert missing.returncode == 3
    assert "SNAPSHOT_NOT_FOUND" in missing.stderr


def test_html_snapshot_correction_changes_the_decision(workspace) -> None:
    original_text = (FIXTURE / "detail-2026-01.html").read_text(encoding="utf-8")
    corrected_text = original_text.replace(
        'data-query="Q1" data-members="A B" data-positive-count="1"',
        'data-query="Q1" data-members="A B" data-positive-count="2"',
    )
    corrected = workspace / "detail-2026-01.html"
    corrected.write_text(corrected_text, encoding="utf-8")
    item = inspect_query_snapshot(corrected, root=workspace)
    receipt = decide_query_release(item, CONTRACT_COMPOSITION)
    assert receipt["approved_query_ids"] == ["Q3"]
    stale = decide_query_release(evidence("detail-2026-01.html"),
                                 CONTRACT_COMPOSITION)
    assert receipt["snapshot_sha256"] != stale["snapshot_sha256"]
    with pytest.raises(QueryReleaseError, match="does not match source snapshot"):
        verify_query_receipt(stale, item, expected_contract=CONTRACT_COMPOSITION)


def test_cli_verify_rechecks_a_saved_receipt(workspace) -> None:
    import json
    import subprocess
    import sys

    receipt_path = workspace / "receipt.json"
    produced = subprocess.run(
        [sys.executable, "-m", "selregress", "query-release",
         "--snapshot", "fixtures/query_privacy/detail-2026-02.html",
         "--contract", "composition_safe", "--out", str(receipt_path)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert produced.returncode == 0, produced.stderr
    verified = subprocess.run(
        [sys.executable, "-m", "selregress", "query-verify",
         "--snapshot", "fixtures/query_privacy/detail-2026-02.html",
         "--receipt", str(receipt_path), "--contract", "composition_safe"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert verified.returncode == 0, verified.stderr
    payload = json.loads(verified.stdout)
    assert payload == {"command": "query-verify", "verified": True,
                       "contract": "composition_safe", "release_id": "R-02"}
    wrong_contract = subprocess.run(
        [sys.executable, "-m", "selregress", "query-verify",
         "--snapshot", "fixtures/query_privacy/detail-2026-02.html",
         "--receipt", str(receipt_path), "--contract", "independent_card"],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert wrong_contract.returncode == 3
    assert "QUERY_RELEASE_INVALID" in wrong_contract.stderr
    tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
    tampered["approved_utility_total"] = 999
    receipt_path.write_text(json.dumps(tampered), encoding="utf-8")
    forged = subprocess.run(
        [sys.executable, "-m", "selregress", "query-verify",
         "--snapshot", "fixtures/query_privacy/detail-2026-02.html",
         "--receipt", str(receipt_path)],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=120,
    )
    assert forged.returncode == 3
    assert "QUERY_RELEASE_INVALID" in forged.stderr
