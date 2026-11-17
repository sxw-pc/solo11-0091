import json
from pathlib import Path

from selregress.table_evidence import inspect_table_contract
from selregress.util import PROJECT_ROOT


def test_table_contract_evidence_has_real_snapshot_but_no_records():
    result = inspect_table_contract(PROJECT_ROOT / "fixtures/table_contract/manifest.json")
    assert result["top_level_rows"] == 4
    assert result["field_hits"]["group_selector"] == 2
    assert result["field_hits"]["item_selector"] == 5  # nested table is visible to global CSS matching
    assert result["contracts"] == ["carry_rowspan", "explicit_only"]
    assert result["records"] is None
    assert len(result["sha256"]) == 64
