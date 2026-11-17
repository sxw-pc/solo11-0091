"""Local HTML aggregate claims and internal composition diagnostics."""
from __future__ import annotations

from itertools import product
from pathlib import Path
from typing import Any

from .snapshot_reader import read_snapshot
from .util import PROJECT_ROOT


class QueryEvidenceError(ValueError):
    pass


def inspect_query_snapshot(path: str | Path, *, version: str = "2026-01",
                           root: Path = PROJECT_ROOT) -> dict[str, Any]:
    snapshot = read_snapshot(Path(path), site="query.invalid", page_type="detail",
                             version=version, root=root)
    main = snapshot.soup.select_one("main[data-release]")
    if main is None or not main.get("data-persons"):
        raise QueryEvidenceError("aggregate release identity missing")
    people = main["data-persons"].split()
    if len(people) < 3 or len(people) > 12 or len(set(people)) != len(people):
        raise QueryEvidenceError("invalid person universe")
    queries = []
    seen = set()
    for row in main.select("#queries tr[data-query]"):
        query_id = row.get("data-query")
        members = (row.get("data-members") or "").split()
        try:
            count = int(row["data-positive-count"])
            utility = int(row["data-utility"])
        except (KeyError, ValueError) as exc:
            raise QueryEvidenceError("invalid aggregate count or utility") from exc
        if (not query_id or query_id in seen or len(members) < 2
                or len(set(members)) != len(members) or not set(members) <= set(people)
                or count <= 0 or count >= len(members) or utility <= 0):
            raise QueryEvidenceError("duplicate or invalid query card")
        seen.add(query_id)
        queries.append({"id": query_id, "members": members,
                        "positive_count": count, "utility": utility})
    if len(queries) < 3:
        raise QueryEvidenceError("too few aggregate queries")
    return {
        "release_id": main["data-release"],
        "snapshot_path": snapshot.meta.path,
        "snapshot_sha256": snapshot.meta.sha256,
        "persons": people,
        "queries": queries,
        "independent_release_decision": None,
        "composition_safe_decision": None,
    }


def preview_query_set(evidence: dict[str, Any], selected: list[str]) -> dict[str, Any]:
    cards = {query["id"]: query for query in evidence["queries"]}
    if len(set(selected)) != len(selected) or not set(selected) <= set(cards):
        raise QueryEvidenceError("invalid selected query IDs")
    people = evidence["persons"]
    possible = {person: set() for person in people}
    worlds = 0
    for assignment in product((0, 1), repeat=len(people)):
        values = dict(zip(people, assignment))
        if any(sum(values[person] for person in cards[query_id]["members"])
               != cards[query_id]["positive_count"] for query_id in selected):
            continue
        worlds += 1
        for person, value in values.items():
            possible[person].add(value)
    return {
        "internal_preview_only": True,
        "publishable": False,
        "selected_query_ids": selected,
        "utility_total": sum(cards[query_id]["utility"] for query_id in selected),
        "consistent_worlds": worlds,
        "possible_values": {person: sorted(values) for person, values in possible.items()},
        "all_people_ambiguous": worlds > 0 and all(values == {0, 1} for values in possible.values()),
    }
