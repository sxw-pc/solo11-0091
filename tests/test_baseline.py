import json
from copy import deepcopy
from pathlib import Path

import pytest

from selregress.baseline import Baseline, compare_baseline, latest_outcomes, load_baseline
from selregress.config import DEFAULT_RULES
from selregress.recipe_validator import load_recipe
from selregress.util import PROJECT_ROOT, InputError, sha256_file


def test_load_baseline():
    baseline = load_baseline("configs/baselines/site_a_detail.baseline.json", PROJECT_ROOT)
    assert baseline.site == "site-a.invalid"
    assert baseline.version == "2024-01"
    assert baseline.fields["price"]["normalized_value"] == "1280"
    assert len(baseline.path) > 0


def test_latest_outcomes_selects_highest_version(shared_execution):
    outcomes = [x for x in shared_execution.result.outcomes if x.recipe_name == "site_a_detail"]
    latest = latest_outcomes(outcomes)
    assert latest["stock"].version == "2024-02"
    assert latest["name"].extraction.normalized_value == "示例甲型号 A"


def test_compare_shared_execution_produces_all_observation_types(shared_execution):
    recipe = load_recipe("configs/recipes/site_a_detail.yaml", PROJECT_ROOT)
    baseline = load_baseline(recipe.baseline, PROJECT_ROOT)
    outcomes = [x for x in shared_execution.result.outcomes if x.recipe_name == recipe.name]
    diffs = compare_baseline(recipe, baseline, outcomes, DEFAULT_RULES)
    types = {item.change_type for item in diffs}
    assert {"value_changed", "match_count_changed", "type_status_changed", "stability_changed"} <= types
    changed = [item for item in diffs if item.field_name == "sku"][0]
    assert changed.old_value == "SKU-A-001"
    assert changed.new_value == "SKU-A-002"
    assert changed.severity == "warn"


def test_added_and_missing_fields(shared_execution):
    recipe = load_recipe("configs/recipes/site_a_detail.yaml", PROJECT_ROOT)
    baseline = load_baseline(recipe.baseline, PROJECT_ROOT)
    baseline = deepcopy(baseline)
    baseline.fields.pop("description")
    baseline.fields["ghost_field"] = {
        "normalized_value": "ghost", "match_count": 1, "conversion_status": "ok", "score": 100,
    }
    outcomes = [x for x in shared_execution.result.outcomes if x.recipe_name == recipe.name]
    diffs = compare_baseline(recipe, baseline, outcomes, DEFAULT_RULES)
    assert any(item.change_type == "added_field" and item.field_name == "description" for item in diffs)
    assert any(item.change_type == "missing_field" and item.field_name == "ghost_field" for item in diffs)


def test_scope_mismatch_is_rejected(shared_execution):
    recipe = load_recipe("configs/recipes/site_a_detail.yaml", PROJECT_ROOT)
    baseline = load_baseline(recipe.baseline, PROJECT_ROOT)
    wrong = Baseline(
        path=baseline.path, site="site-b.invalid", page_type="detail",
        version="2024-01", generated_at="", fields=baseline.fields,
    )
    with pytest.raises(InputError) as caught:
        compare_baseline(recipe, wrong, [], DEFAULT_RULES)
    assert caught.value.code == "BASELINE_SCOPE_MISMATCH"


def test_invalid_baseline_json(workspace):
    path = workspace / "bad.json"
    path.write_text('{"schema_version": 1, "fields": {}}', encoding="utf-8")
    with pytest.raises(InputError) as caught:
        load_baseline(path, PROJECT_ROOT)
    assert caught.value.code == "BASELINE_INVALID"
