from dataclasses import replace
from pathlib import Path

import pytest

from selregress.config import load_config
from selregress.loader import load_bundle
from selregress.util import PROJECT_ROOT, InputError


def test_bundle_loads_sorted_recipes_and_snapshots(shared_config):
    bundle = load_bundle(shared_config)
    assert [item.name for item in bundle.recipes] == [
        "site_a_detail", "site_a_list", "site_b_detail",
    ]
    assert len(bundle.snapshots) == 6
    assert bundle.warnings == []
    assert bundle.snapshots[0].meta.site == "site-a.invalid"


def test_bundle_hashes_are_stable(shared_config):
    first = load_bundle(shared_config)
    second = load_bundle(shared_config)
    assert [(item.meta.path, item.meta.sha256) for item in first.snapshots] == [
        (item.meta.path, item.meta.sha256) for item in second.snapshots
    ]
    assert [item.sha256 for item in first.recipes] == [item.sha256 for item in second.recipes]


def test_recipe_without_snapshot_produces_warning(shared_config, workspace):
    copied = workspace / "recipe.yaml"
    copied.write_text(
        (PROJECT_ROOT / "configs/recipes/site_a_list.yaml").read_text(encoding="utf-8")
        .replace("site-a.invalid", "site-z.invalid"),
        encoding="utf-8",
    )
    config = replace(shared_config, recipe_paths=(copied,))
    bundle = load_bundle(config)
    assert any("没有匹配的快照" in item for item in bundle.warnings)


def test_missing_recipe_file_is_input_error(shared_config):
    config = replace(shared_config, recipe_paths=(PROJECT_ROOT / "configs/recipes/nope.yaml",))
    with pytest.raises(InputError) as caught:
        load_bundle(config)
    assert caught.value.code == "RECIPE_NOT_FOUND"


def test_duplicate_recipe_name_is_rejected(shared_config, workspace):
    source = PROJECT_ROOT / "configs/recipes/site_a_list.yaml"
    first = workspace / "one.yaml"
    second = workspace / "two.yaml"
    text = source.read_text(encoding="utf-8")
    first.write_text(text, encoding="utf-8")
    second.write_text(text, encoding="utf-8")
    config = replace(shared_config, recipe_paths=(first, second))
    with pytest.raises(InputError) as caught:
        load_bundle(config)
    assert caught.value.code == "RECIPE_DUPLICATE_NAME"


def test_config_path_outside_root_is_rejected():
    with pytest.raises(InputError) as caught:
        load_config(Path("C:/outside-demo.yaml"), PROJECT_ROOT)
    assert caught.value.code == "PATH_OUTSIDE_ROOT"
