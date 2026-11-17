from __future__ import annotations

from pathlib import Path

import pytest

from selregress.journey_evidence import JourneyEvidenceError, inspect_journey_contract

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "journey" / "contract.json"


def test_local_html_schedule_and_scenarios_are_bound() -> None:
    result = inspect_journey_contract(FIXTURE)
    assert [s["rows"] for s in result["snapshots"]] == [3, 2]
    assert {t["trip_id"] for t in result["trips"]} == {"X1", "X2", "X3", "Y1", "Y2"}
    assert [s["weight_pct"] for s in result["scenarios"]] == [40, 60]
    assert result["contingent_itineraries"] is None
    assert result["contract_choices"] is None
    assert result == inspect_journey_contract(FIXTURE)


def test_invalid_scenario_weights_are_rejected() -> None:
    with pytest.raises(JourneyEvidenceError, match="total 100"):
        inspect_journey_contract(FIXTURE.with_name("contract-bad-weight.json"))
