"""Parse local HTML barcode fragments and validate a supplied assembly."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from .snapshot_reader import read_snapshot
from .util import PROJECT_ROOT


class BarcodeEvidenceError(ValueError):
    pass


def inspect_barcode_snapshot(path: str | Path, *, version: str = "2026-01",
                             root: Path = PROJECT_ROOT) -> dict[str, Any]:
    snapshot = read_snapshot(Path(path), site="barcode.invalid", page_type="detail",
                             version=version, root=root)
    main = snapshot.soup.select_one("main[data-barcode]")
    if main is None:
        raise BarcodeEvidenceError("barcode identity missing")
    alphabet = set("ACGT")
    barcode = main.get("data-barcode")
    start, end, marker = main.get("data-start"), main.get("data-end"), main.get("data-marker")
    if (not barcode or start not in alphabet or end not in alphabet
            or not marker or len(marker) != 3 or not set(marker) <= alphabet):
        raise BarcodeEvidenceError("barcode contract invalid")
    fragments = []
    seen = set()
    for row in main.select("#fragments [data-fragment]"):
        fragment = row.get("data-fragment")
        try:
            count = int(row["data-count"])
        except (KeyError, ValueError) as exc:
            raise BarcodeEvidenceError("invalid fragment count") from exc
        if (not fragment or len(fragment) != 2 or not set(fragment) <= alphabet
                or fragment in seen or count <= 0):
            raise BarcodeEvidenceError("duplicate or invalid fragment")
        seen.add(fragment)
        fragments.append({"fragment": fragment, "count": count})
    if len(fragments) < 4:
        raise BarcodeEvidenceError("too few fragments")
    return {
        "barcode_id": barcode,
        "snapshot_path": snapshot.meta.path,
        "snapshot_sha256": snapshot.meta.sha256,
        "start": start,
        "end": end,
        "marker": marker,
        "fragments": fragments,
        "required_length": 1 + sum(row["count"] for row in fragments),
        "possible_marker_decision": None,
        "guaranteed_marker_decision": None,
    }


def preview_candidate_assembly(evidence: dict[str, Any], assembled: str) -> dict[str, Any]:
    wanted = Counter({row["fragment"]: row["count"] for row in evidence["fragments"]})
    actual = Counter(assembled[index:index + 2] for index in range(len(assembled) - 1))
    valid = (len(assembled) == evidence["required_length"]
             and assembled.startswith(evidence["start"])
             and assembled.endswith(evidence["end"])
             and set(assembled) <= set("ACGT")
             and actual == wanted)
    return {
        "internal_preview_only": True,
        "publishable": False,
        "assembled": assembled,
        "valid": valid,
        "contains_marker": evidence["marker"] in assembled if valid else None,
    }
