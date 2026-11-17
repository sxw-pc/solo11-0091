"""Load local timetable snapshots with the same selector machinery as recipes.

The baseline validates input identities and scenario weights. It deliberately
does not choose an initial trip or a contingent transfer policy.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .matcher import match_nodes
from .models import FieldSpec
from .util import PROJECT_ROOT, sha256_file


class JourneyEvidenceError(ValueError):
    pass


def _minutes(value: Any) -> int:
    if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
        raise JourneyEvidenceError("invalid local timetable time")
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def inspect_journey_contract(contract_path: str | Path) -> dict[str, Any]:
    contract_file = Path(contract_path).resolve()
    contract_file.relative_to(PROJECT_ROOT.resolve())
    contract = json.loads(contract_file.read_text(encoding="utf-8"))
    if (contract.get("schema_version") != 1 or contract.get("initial_choice_before_scenario_observed") is not True
            or contract.get("rebook_after_observed_arrival") is not True):
        raise JourneyEvidenceError("unsupported journey observation contract")
    selector = contract.get("trip_selector")
    paths = contract.get("snapshot_files")
    expected_hashes = contract.get("snapshot_sha256")
    if (not isinstance(selector, str) or not isinstance(paths, list) or len(paths) != 2
            or not isinstance(expected_hashes, dict) or set(expected_hashes) != set(paths)):
        raise JourneyEvidenceError("missing local timetable snapshots")
    snapshots = []
    trips = []
    trip_ids = set()
    for relative in paths:
        snapshot = (PROJECT_ROOT / relative).resolve()
        snapshot.relative_to(PROJECT_ROOT.resolve())
        actual_hash = sha256_file(snapshot)
        if actual_hash != expected_hashes[relative]:
            raise JourneyEvidenceError("snapshot SHA does not match contract")
        soup = BeautifulSoup(snapshot.read_text(encoding="utf-8"), "html.parser")
        spec = FieldSpec(name="trip_row", selectors=(selector,))
        selected = match_nodes(soup, spec)
        if selected.count != len(soup.select(selector)) or selected.count == 0:
            raise JourneyEvidenceError("selector does not find timetable trips")
        snapshots.append({"file": relative, "sha256": actual_hash, "rows": selected.count})
        for row in soup.select(selector):
            trip = {"trip_id": row.get("data-trip"), "origin": row.get("data-from"),
                    "destination": row.get("data-to"), "depart": row.get("data-depart"),
                    "arrive": row.get("data-arrive"), "snapshot": relative}
            if (not all(isinstance(trip[key], str) and trip[key] for key in ("trip_id", "origin", "destination"))
                    or trip["trip_id"] in trip_ids or _minutes(trip["depart"]) >= _minutes(trip["arrive"])):
                raise JourneyEvidenceError("invalid or duplicate timetable trip")
            trip_ids.add(trip["trip_id"])
            trips.append(trip)
    if (not isinstance(contract.get("origin"), str) or not isinstance(contract.get("destination"), str)
            or _minutes(contract.get("ready_time")) >= _minutes(contract.get("deadline"))
            or type(contract.get("minimum_transfer_minutes")) is not int
            or contract["minimum_transfer_minutes"] < 0):
        raise JourneyEvidenceError("invalid journey boundary")
    scenarios = contract.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise JourneyEvidenceError("missing delay scenarios")
    scenario_ids = set()
    weight_sum = 0
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("scenario_id"), str):
            raise JourneyEvidenceError("invalid delay scenario")
        sid = scenario["scenario_id"]
        weight = scenario.get("weight_pct")
        delays = scenario.get("arrival_delay_minutes")
        if (sid in scenario_ids or type(weight) is not int or weight <= 0
                or not isinstance(delays, dict) or set(delays) != trip_ids
                or any(type(minutes) is not int or minutes < 0 for minutes in delays.values())):
            raise JourneyEvidenceError("scenario weight or trip identities differ")
        scenario_ids.add(sid)
        weight_sum += weight
    if weight_sum != 100:
        raise JourneyEvidenceError("scenario weights must total 100")
    contracts = contract.get("travel_contracts")
    if (not isinstance(contracts, list) or {x.get("kind") for x in contracts}
            != {"expected_fastest", "guaranteed_by_deadline"}):
        raise JourneyEvidenceError("missing travel contracts")
    return {"snapshots": snapshots, "trips": trips, "origin": contract["origin"],
            "destination": contract["destination"], "ready_time": contract["ready_time"],
            "deadline": contract["deadline"], "minimum_transfer_minutes": contract["minimum_transfer_minutes"],
            "initial_choice_before_scenario_observed": True, "rebook_after_observed_arrival": True,
            "scenarios": scenarios, "travel_contracts": contracts,
            "contingent_itineraries": None, "contract_choices": None}
