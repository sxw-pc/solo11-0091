"""输入装载：串起配置、配方与本地快照。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig
from .models import Recipe
from .recipe_validator import load_recipe
from .snapshot_reader import LoadedSnapshot, load_snapshots
from .util import InputError, PROJECT_ROOT


@dataclass(slots=True)
class InputBundle:
    recipes: list[Recipe]
    snapshots: list[LoadedSnapshot]
    warnings: list[str] = field(default_factory=list)
    root: Path = PROJECT_ROOT


def load_bundle(config: AppConfig) -> InputBundle:
    recipes: list[Recipe] = []
    names: set[str] = set()
    for path in config.recipe_paths:
        recipe = load_recipe(path, config.root)
        if recipe.name in names:
            raise InputError("RECIPE_DUPLICATE_NAME", f"配方名称重复: {recipe.name}")
        names.add(recipe.name)
        recipes.append(recipe)
    recipes.sort(key=lambda item: item.name)
    snapshots, warnings = load_snapshots(config.snapshot_dir, config.root)
    if not snapshots:
        raise InputError("NO_SNAPSHOTS", "快照目录中没有可用的 HTML 快照")
    known = {(item.meta.site, item.meta.page_type) for item in snapshots}
    for recipe in recipes:
        if (recipe.site, recipe.page_type) not in known:
            warnings.append(
                f"配方 {recipe.name} 没有匹配的快照: {recipe.site}/{recipe.page_type}"
            )
    snapshots.sort(key=lambda item: (
        item.meta.site, item.meta.page_type, item.meta.version, item.meta.path
    ))
    return InputBundle(
        recipes=recipes, snapshots=snapshots, warnings=sorted(warnings), root=config.root
    )
