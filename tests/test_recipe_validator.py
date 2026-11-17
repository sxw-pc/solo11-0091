from copy import deepcopy

import yaml

from selregress.recipe_validator import (
    RecipeValidationError, load_recipe, validate_recipe_data,
)
from selregress.util import PROJECT_ROOT


def valid_data():
    return {
        "schema_version": 1,
        "name": "unit_recipe",
        "site": "site-a.invalid",
        "page_type": "detail",
        "fields": [
            {"name": "title", "selector": "h1.title", "type": "string", "required": True},
            {"name": "tags", "selectors": [".tag", "[data-tag]"], "multi": True, "separator": " | "},
        ],
    }


def codes(data):
    recipe, issues = validate_recipe_data(data, root=PROJECT_ROOT)
    assert recipe is None
    return {item.code for item in issues}


def test_load_valid_recipe_and_baseline():
    recipe = load_recipe("configs/recipes/site_a_detail.yaml", PROJECT_ROOT)
    assert recipe.name == "site_a_detail"
    assert recipe.baseline == "configs/baselines/site_a_detail.baseline.json"
    assert len(recipe.fields) == 8
    assert recipe.fields[0].selectors == ("h1.product-name",)


def test_selectors_list_and_multi_are_accepted():
    recipe, issues = validate_recipe_data(valid_data(), root=PROJECT_ROOT)
    assert issues == []
    assert recipe is not None
    assert recipe.fields[1].selectors == (".tag", "[data-tag]")
    assert recipe.fields[1].multi is True


def test_malformed_recipe_reports_stable_codes():
    _, issues = validate_recipe_data(
        yaml.safe_load((PROJECT_ROOT / "fixtures/bad/malformed_recipe.yaml").read_text(encoding="utf-8")),
        root=PROJECT_ROOT,
    )
    found = {item.code for item in issues}
    assert {"R002_UNKNOWN_KEY", "R006_DUPLICATE_FIELD", "R007_INVALID_FIELD_TYPE"} <= found
    assert "R008_EMPTY_SELECTOR" in found
    assert "R015_INVALID_REQUIRED" in found


def test_missing_name_and_fields_are_rejected():
    data = valid_data()
    del data["name"]
    data["fields"] = []
    found = codes(data)
    assert "R003_MISSING_KEY" in found


def test_selector_conflict_and_invalid_css():
    data = valid_data()
    data["fields"][0]["selectors"] = [".x"]
    found = codes(data)
    assert "R008_SELECTOR_CONFLICT" in found
    data = valid_data()
    data["fields"][0]["selector"] = "div[["
    assert "R009_INVALID_SELECTOR" in codes(data)


def test_attr_and_normalize_validation():
    data = valid_data()
    data["fields"][0]["extract"] = "attr"
    found = codes(data)
    assert "R010_ATTRIBUTE_REQUIRED" in found
    data = valid_data()
    data["fields"][0]["normalize"] = [{"type": "regex_replace", "pattern": "[", "replacement": ""}]
    assert "R011_INVALID_NORMALIZE" in codes(data)
    data = valid_data()
    data["fields"][0]["normalize"] = ["not_a_rule"]
    assert "R011_INVALID_NORMALIZE" in codes(data)


def test_baseline_missing_and_outside_root():
    data = valid_data()
    data["baseline"] = "configs/baselines/not-there.json"
    assert "R012_BASELINE_NOT_FOUND" in codes(data)
    data["baseline"] = "../outside.json"
    assert "R013_BASELINE_OUTSIDE_ROOT" in codes(data)


def test_invalid_separator_and_on_change():
    data = valid_data()
    data["fields"][1]["separator"] = ""
    data["fields"][0]["on_change"] = "sometimes"
    found = codes(data)
    assert "R014_INVALID_SEPARATOR" in found
    assert "R017_INVALID_ON_CHANGE" in found


def test_loader_aggregates_issues():
    try:
        load_recipe("fixtures/bad/malformed_recipe.yaml", PROJECT_ROOT)
    except RecipeValidationError as exc:
        assert exc.code == "RECIPE_INVALID"
        assert len(exc.issues) >= 4
        assert exc.details["issues"][0]["code"].startswith("R")
    else:
        raise AssertionError("expected RecipeValidationError")
