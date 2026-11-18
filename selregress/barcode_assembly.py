"""Decide marker contracts over the full assembly space of a barcode snapshot.

``selregress.barcode_evidence`` only previews caller-supplied candidates and
leaves ``possible_marker_decision`` / ``guaranteed_marker_decision`` as
``None``.  This module is the formal adjudicator for the two report
contracts of one parsed snapshot:

* ``possible_marker`` -- prove that at least one legal complete assembly
  contains the marker, and emit the full string plus a fragment usage
  receipt as the existence witness.
* ``guaranteed_marker`` -- prove that no legal complete assembly avoids the
  marker (a universal proof backed by a search-exhaustion certificate), or
  emit one legal counterexample.

Every two-character fragment is a directed multiedge.  Fragments with
identical text stay parallel counted edges: they are never deduplicated and
never permuted as distinct copies, so high multiplicities are searched as
count vectors instead of an edge-permutation blow-up.  Search states are
``(vertex, remaining counts, marker automaton state)``: a single Hierholzer
trail cannot stand in for all assemblies, and locally present ``C -> A`` /
``A -> G`` edges only count as adjacent when the automaton state says so.

If no complete assembly exists at all, both contracts report
``decision=False`` with ``reason="no_complete_assembly"`` -- the vacuous
universal truth of the empty assembly set is never reported as a verified
barcode.  All inputs are local synthetic barcode strings.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence

from .barcode_evidence import (
    BarcodeEvidenceError,
    inspect_barcode_snapshot,
    preview_candidate_assembly,
)
from .snapshot_reader import SNAPSHOT_NAME
from .util import InputError, canonical_digest, stable_json_text

ALPHABET = frozenset("ACGT")
CONTRACTS: tuple[str, str] = ("possible_marker", "guaranteed_marker")
REPORT_TYPE = "barcode_assembly_adjudication"
REASON_NO_ASSEMBLY = "no_complete_assembly"

_SEARCH_MODES = ("any", "with_marker", "avoid_marker")


class BarcodeAssemblyError(ValueError):
    """非法的组装合同输入，或裁决内部不一致。"""


@dataclass(frozen=True)
class AssemblyProblem:
    """Validated count-based directed multigraph view of one snapshot."""

    barcode_id: str
    start: str
    end: str
    marker: str
    fragments: tuple[str, ...]  # sorted unique two-character texts
    counts: tuple[int, ...]  # parallel to fragments

    @property
    def edge_count(self) -> int:
        return sum(self.counts)


def _require_alphabet(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or not set(value) <= ALPHABET:
        raise BarcodeAssemblyError(f"illegal {label}: {value!r}")
    return value


def build_assembly_problem(evidence: dict[str, Any]) -> AssemblyProblem:
    """Validate parsed evidence into a multigraph; reject illegal characters."""
    if not isinstance(evidence, dict):
        raise BarcodeAssemblyError("evidence must be a mapping")
    try:
        start = evidence["start"]
        end = evidence["end"]
        marker = evidence["marker"]
        rows = evidence["fragments"]
    except KeyError as exc:
        raise BarcodeAssemblyError(f"evidence missing field: {exc.args[0]}") from exc
    start = _require_alphabet(start, "start anchor")
    end = _require_alphabet(end, "end anchor")
    if len(start) != 1 or len(end) != 1:
        raise BarcodeAssemblyError("start/end anchors must be single characters")
    marker = _require_alphabet(marker, "marker")
    if len(marker) < 2:
        raise BarcodeAssemblyError("marker must contain at least two characters")
    if not isinstance(rows, (list, tuple)) or not rows:
        raise BarcodeAssemblyError("evidence fragments must be a non-empty list")
    seen: set[str] = set()
    pairs: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BarcodeAssemblyError("fragment row must be a mapping")
        text = row.get("fragment")
        count = row.get("count")
        if not isinstance(text, str) or len(text) != 2 or not set(text) <= ALPHABET:
            raise BarcodeAssemblyError(f"illegal fragment: {text!r}")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise BarcodeAssemblyError(f"illegal count for fragment {text}: {count!r}")
        if text in seen:
            raise BarcodeAssemblyError(f"duplicate fragment: {text}")
        seen.add(text)
        pairs.append((text, count))
    pairs.sort()
    barcode_id = evidence.get("barcode_id")
    return AssemblyProblem(
        barcode_id=str(barcode_id) if barcode_id is not None else "",
        start=start,
        end=end,
        marker=marker,
        fragments=tuple(text for text, _ in pairs),
        counts=tuple(count for _, count in pairs),
    )


def _marker_automaton(marker: str) -> tuple[dict[str, int], ...]:
    """KMP-style transition table; state ``len(marker)`` is the absorbing hit."""
    table = []
    for state in range(len(marker) + 1):
        row: dict[str, int] = {}
        for ch in sorted(ALPHABET):
            if state == len(marker):
                row[ch] = state
                continue
            candidate = marker[:state] + ch
            nxt = 0
            for k in range(min(len(marker), state + 1), 0, -1):
                if candidate.endswith(marker[:k]):
                    nxt = k
                    break
            row[ch] = nxt
        table.append(row)
    return tuple(table)


def _encode_state(state: tuple[Any, ...]) -> list[Any]:
    return [state[0], list(state[1]), *state[2:]]


def _sorted_states(memo: dict[tuple[Any, ...], bool]) -> list[tuple[Any, ...]]:
    return sorted(memo, key=lambda s: json.dumps(_encode_state(s), sort_keys=True))


def _state_digest(memo: dict[tuple[Any, ...], bool]) -> str:
    encoded = [json.dumps(_encode_state(s), sort_keys=True) for s in _sorted_states(memo)]
    return canonical_digest(encoded)


class _AssemblySearch:
    """Exhaustive count-based search over complete assemblies.

    The remaining multiset strictly shrinks along every transition, so the
    state graph is a DAG and one memoized post-order traversal is exact.
    Modes: ``any`` (plain existence), ``with_marker`` (must contain the
    marker), ``avoid_marker`` (transitions completing the marker are cut).
    """

    def __init__(self, problem: AssemblyProblem, mode: str) -> None:
        if mode not in _SEARCH_MODES:
            raise ValueError(f"unknown search mode: {mode}")
        self.problem = problem
        self.mode = mode
        self.automaton = _marker_automaton(problem.marker)
        self.memo: dict[tuple[Any, ...], bool] = {}

    def _initial(self) -> tuple[Any, ...]:
        if self.mode == "any":
            return (self.problem.start, self.problem.counts)
        matched = 1 if self.problem.marker[0] == self.problem.start else 0
        return (self.problem.start, self.problem.counts, matched)

    def _terminal_success(self, state: tuple[Any, ...]) -> bool:
        vertex, remaining = state[0], state[1]
        if any(remaining) or vertex != self.problem.end:
            return False
        if self.mode == "with_marker":
            return state[2] == len(self.problem.marker)
        return True

    def _transitions(self, state: tuple[Any, ...]) -> Iterator[tuple[str, tuple[Any, ...]]]:
        vertex, remaining = state[0], state[1]
        for index, text in enumerate(self.problem.fragments):
            if remaining[index] <= 0 or text[0] != vertex:
                continue
            child_remaining = remaining[:index] + (remaining[index] - 1,) + remaining[index + 1:]
            if self.mode == "any":
                yield text, (text[1], child_remaining)
                continue
            nxt = self.automaton[state[2]][text[1]]
            if self.mode == "avoid_marker" and nxt == len(self.problem.marker):
                continue  # 该转移会补全标记，规避搜索中禁止
            yield text, (text[1], child_remaining, nxt)

    def solve(self) -> bool:
        initial = self._initial()
        memo = self.memo
        stack: list[tuple[tuple[Any, ...], bool]] = [(initial, False)]
        while stack:
            state, expanded = stack.pop()
            if state in memo:
                continue
            if not expanded:
                stack.append((state, True))
                for _, child in self._transitions(state):
                    if child not in memo:
                        stack.append((child, False))
                continue
            if not any(state[1]):
                memo[state] = self._terminal_success(state)
            else:
                memo[state] = any(memo.get(child, False) for _, child in self._transitions(state))
        return memo.get(initial, False)

    def reconstruct(self) -> str:
        """Replay the memo table into one deterministic complete assembly."""
        state = self._initial()
        if not self.memo.get(state):
            raise BarcodeAssemblyError("no witness path exists for this search")
        chars = [self.problem.start]
        while any(state[1]):
            for text, child in self._transitions(state):
                if self.memo.get(child):
                    chars.append(text[1])
                    state = child
                    break
            else:
                raise BarcodeAssemblyError("memoized search lost its witness path")
        if not self._terminal_success(state):
            raise BarcodeAssemblyError("reconstructed path is not a complete assembly")
        return "".join(chars)


def _assembly_receipt(problem: AssemblyProblem, assembled: str, role: str) -> dict[str, Any]:
    usage = Counter(assembled[index:index + 2] for index in range(len(assembled) - 1))
    expected = dict(zip(problem.fragments, problem.counts))
    actual = dict(sorted(usage.items()))
    return {
        "role": role,
        "assembled": assembled,
        "length": len(assembled),
        "start": assembled[0],
        "end": assembled[-1],
        "contains_marker": problem.marker in assembled,
        "fragment_usage": actual,
        "usage_matches_evidence": actual == expected,
    }


def _search_summary(search: _AssemblySearch) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "states_explored": len(search.memo),
        "state_digest": _state_digest(search.memo),
    }
    if len(search.memo) <= 64:
        summary["explored_states"] = [_encode_state(s) for s in _sorted_states(search.memo)]
    return summary


def _exhaustion_certificate(
    search: _AssemblySearch,
    claim: str,
    structural_notes: Sequence[str] = (),
) -> dict[str, Any]:
    return {
        "certificate_type": "search_exhaustion",
        "claim": claim,
        "states_explored": len(search.memo),
        "state_digest": _state_digest(search.memo),
        "state_model": "(vertex, remaining fragment counts, marker automaton state)",
        "multiplicity_model": "identical fragments are counted multiplicities, never permuted as distinct copies",
        "structural_summary": list(structural_notes),
    }


def _structural_notes(problem: AssemblyProblem) -> list[str]:
    """Explain a forced marker when every marker-arming edge forces its completion."""
    marker = problem.marker
    if len(marker) != 3:
        return []
    bridge, tail = marker[:2], marker[1:]
    mid = marker[1]
    counts = dict(zip(problem.fragments, problem.counts))
    if counts.get(bridge, 0) <= 0:
        return []
    outgoing = {text for text in problem.fragments if text[0] == mid and counts[text] > 0}
    if outgoing != {tail} or problem.end == mid:
        return []
    return [
        f"every '{bridge}' edge enters vertex '{mid}', whose only outgoing fragment "
        f"type is '{tail}', and '{bridge}' + '{tail}' completes marker '{marker}'",
        f"complete assemblies end at '{problem.end}' (not '{mid}'), so a '{bridge}' "
        f"edge is never the last edge of a complete assembly",
        f"therefore every complete assembly contains marker '{marker}'",
    ]


def _evidence_digest(problem: AssemblyProblem, evidence: dict[str, Any]) -> str:
    return canonical_digest(
        {
            "barcode_id": problem.barcode_id,
            "start": problem.start,
            "end": problem.end,
            "marker": problem.marker,
            "fragments": [
                {"fragment": text, "count": count}
                for text, count in zip(problem.fragments, problem.counts)
            ],
            "snapshot_sha256": evidence.get("snapshot_sha256"),
        }
    )


def evidence_digest(evidence: dict[str, Any]) -> str:
    """Stable digest of the validated contract input plus its snapshot SHA."""
    return _evidence_digest(build_assembly_problem(evidence), evidence)


def adjudicate_assembly(evidence: dict[str, Any]) -> dict[str, Any]:
    """Decide both marker contracts and emit witnesses or certificates."""
    problem = build_assembly_problem(evidence)
    any_search = _AssemblySearch(problem, "any")
    exists = any_search.solve()
    with_search = _AssemblySearch(problem, "with_marker")
    avoid_search = _AssemblySearch(problem, "avoid_marker")
    possible = with_search.solve() if exists else False
    avoiding = avoid_search.solve() if exists else False

    possible_block: dict[str, Any]
    guaranteed_block: dict[str, Any]
    if not exists:
        possible_block = {
            "decision": False,
            "reason": REASON_NO_ASSEMBLY,
            "witness": None,
            "certificate": None,
        }
        guaranteed_block = {
            "decision": False,
            "reason": REASON_NO_ASSEMBLY,
            "counterexample": None,
            "certificate": None,
            "vacuous_truth_guard": True,
        }
    else:
        if possible:
            witness = with_search.reconstruct()
            possible_block = {
                "decision": True,
                "reason": "witness_found",
                "witness": _assembly_receipt(problem, witness, "existence_witness"),
                "certificate": None,
            }
        else:
            possible_block = {
                "decision": False,
                "reason": "no_assembly_contains_marker",
                "witness": None,
                "certificate": _exhaustion_certificate(
                    with_search, "no_complete_assembly_contains_marker"
                ),
            }
        if avoiding:
            counterexample = avoid_search.reconstruct()
            guaranteed_block = {
                "decision": False,
                "reason": "counterexample_found",
                "counterexample": _assembly_receipt(problem, counterexample, "counterexample"),
                "certificate": None,
            }
        else:
            guaranteed_block = {
                "decision": True,
                "reason": "all_assemblies_contain_marker",
                "counterexample": None,
                "certificate": _exhaustion_certificate(
                    avoid_search,
                    "no_complete_assembly_avoids_marker",
                    _structural_notes(problem),
                ),
            }

    report: dict[str, Any] = {
        "report_type": REPORT_TYPE,
        "barcode_id": problem.barcode_id,
        "snapshot_path": evidence.get("snapshot_path"),
        "snapshot_sha256": evidence.get("snapshot_sha256"),
        "evidence_digest": _evidence_digest(problem, evidence),
        "start": problem.start,
        "end": problem.end,
        "marker": problem.marker,
        "edge_count": problem.edge_count,
        "fragment_types": len(problem.fragments),
        "complete_assembly_exists": exists,
        "contracts": {
            "possible_marker": possible_block,
            "guaranteed_marker": guaranteed_block,
        },
        "search": {
            "complete_assembly": _search_summary(any_search),
            "with_marker": _search_summary(with_search) if exists else {"skipped": True},
            "avoid_marker": _search_summary(avoid_search) if exists else {"skipped": True},
        },
    }
    if not exists:
        report["no_assembly_certificate"] = _exhaustion_certificate(
            any_search, "no_complete_assembly_exists"
        )
    return report


def _receipt_problems(evidence: dict[str, Any], receipt: Any) -> list[str]:
    if not isinstance(receipt, dict):
        return ["receipt is not a mapping"]
    assembled = receipt.get("assembled")
    if not isinstance(assembled, str) or not assembled:
        return ["receipt missing assembled string"]
    problems = []
    try:
        preview = preview_candidate_assembly(evidence, assembled)
    except KeyError as exc:
        return [f"evidence incomplete for receipt verification: {exc.args[0]}"]
    if not preview["valid"]:
        problems.append(f"receipt assembly {assembled!r} is not a legal complete assembly")
    elif preview["contains_marker"] != receipt.get("contains_marker"):
        problems.append("receipt contains_marker disagrees with evidence preview")
    expected = {row["fragment"]: row["count"] for row in evidence["fragments"]}
    if receipt.get("fragment_usage") != expected:
        problems.append("receipt fragment_usage does not match the evidence multiset")
    return problems


def verify_report(evidence: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Re-check a report's snapshot binding, decisions, and receipts."""
    if not isinstance(report, dict) or report.get("report_type") != REPORT_TYPE:
        return {"ok": False, "problems": [f"report_type is not {REPORT_TYPE}"]}
    try:
        expected_digest = evidence_digest(evidence)
    except BarcodeAssemblyError as exc:
        return {"ok": False, "problems": [f"evidence rejected: {exc}"]}
    problems: list[str] = []
    if report.get("snapshot_sha256") != evidence.get("snapshot_sha256"):
        problems.append("snapshot_sha256 mismatch: report is bound to a different snapshot")
    if report.get("evidence_digest") != expected_digest:
        problems.append("evidence_digest mismatch: report was not adjudicated from this evidence")
    fresh = adjudicate_assembly(evidence)
    contracts = report.get("contracts")
    if not isinstance(contracts, dict):
        problems.append("report contracts missing")
        contracts = {}
    for name in CONTRACTS:
        block = contracts.get(name)
        got = block.get("decision") if isinstance(block, dict) else None
        want = fresh["contracts"][name]["decision"]
        if got is not want:
            problems.append(f"{name} decision mismatch: report={got!r} recompute={want!r}")
    for name, key in (("possible_marker", "witness"), ("guaranteed_marker", "counterexample")):
        block = contracts.get(name)
        if not isinstance(block, dict):
            continue
        receipt = block.get(key)
        if receipt is not None:
            problems.extend(_receipt_problems(evidence, receipt))
    return {"ok": not problems, "problems": problems}


def _infer_version(path: Path) -> str:
    match = SNAPSHOT_NAME.match(path.name)
    return match.group(2) if match else "2026-01"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m selregress.barcode_assembly",
        description="裁决本地条码快照的 possible_marker / guaranteed_marker 合同（仅本地合成证据）",
    )
    parser.add_argument("snapshot", help="快照 HTML 路径（必须位于项目根目录内）")
    parser.add_argument(
        "--contract",
        choices=[*CONTRACTS, "both"],
        default="both",
        help="只输出指定合同，默认两者都裁决",
    )
    parser.add_argument("--version", default=None, help="快照版本标签，默认从文件名推断")
    parser.add_argument(
        "--verify",
        metavar="REPORT.json",
        default=None,
        help="改为复核一份既有裁决报告与该快照的绑定",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        snapshot_path = Path(args.snapshot)
        version = args.version or _infer_version(snapshot_path)
        evidence = inspect_barcode_snapshot(snapshot_path, version=version)
        if args.verify:
            try:
                report = json.loads(Path(args.verify).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise InputError("REPORT_UNREADABLE", f"无法读取裁决报告: {exc}") from exc
            result = verify_report(evidence, report)
            sys.stdout.write(stable_json_text(result))
            return 0 if result["ok"] else 2
        adjudication = adjudicate_assembly(evidence)
        if args.contract != "both":
            adjudication["contracts"] = {
                args.contract: adjudication["contracts"][args.contract]
            }
        sys.stdout.write(stable_json_text(adjudication))
        return 0
    except InputError as exc:
        sys.stderr.write(stable_json_text({"error": exc.code, "message": exc.message}))
        return 3
    except (BarcodeEvidenceError, BarcodeAssemblyError) as exc:
        sys.stderr.write(stable_json_text({"error": "BARCODE_CONTRACT", "message": str(exc)}))
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
