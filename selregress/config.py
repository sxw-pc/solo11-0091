"""配置加载与路径边界约束。"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .util import InputError, PROJECT_ROOT, relative_posix, resolve_under_root

DEFAULT_RULES: dict[str, Any] = {
    "default_on_change": "warn",
    "no_match": "warn",
    "multi_match": "warn",
    "type_error": "warn",
    "ambiguous_selector": "warn",
    "version_drift": "warn",
    "empty_value": "warn",
    "added_field": "warn",
    "missing_field": "warn",
    "match_count_change": "warn",
    "type_status_change": "warn",
    "stability_change": "warn",
    "stability_drop_threshold": 10,
    "required_failure": "fail",
}
CONFIG_KEYS = {
    "schema_version", "name", "description", "snapshot_dir", "recipes",
    "ledger_path", "runs_dir", "fixed_time", "rules",
}
SEVERITY_VALUES = {"pass", "info", "warn", "fail", "ignore"}


@dataclass(slots=True)
class AppConfig:
    root: Path
    config_path: Path
    name: str
    snapshot_dir: Path
    recipe_paths: tuple[Path, ...]
    ledger_path: Path
    runs_dir: Path
    fixed_time: str | None
    rules: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_RULES))
    description: str = ""

    @property
    def config_relpath(self) -> str:
        return relative_posix(self.config_path, self.root)

    def with_overrides(
        self,
        *,
        ledger_path: Path | None = None,
        runs_dir: Path | None = None,
        fixed_time: str | None = None,
    ) -> "AppConfig":
        return replace(
            self,
            ledger_path=ledger_path or self.ledger_path,
            runs_dir=runs_dir or self.runs_dir,
            fixed_time=self.fixed_time if fixed_time is None else fixed_time,
        )

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.config_relpath,
            "snapshot_dir": relative_posix(self.snapshot_dir, self.root),
            "ledger_path": relative_posix(self.ledger_path, self.root),
            "runs_dir": relative_posix(self.runs_dir, self.root),
            "rules": dict(sorted(self.rules.items())),
        }


def _require_mapping(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise InputError("CONFIG_INVALID", "配置文件根节点必须是映射")
    return data


def _validate_rules(raw: Any) -> dict[str, Any]:
    rules = dict(DEFAULT_RULES)
    if raw is None:
        return rules
    if not isinstance(raw, dict):
        raise InputError("CONFIG_RULES_INVALID", "rules 必须是映射")
    for key, value in raw.items():
        if key not in DEFAULT_RULES:
            raise InputError("CONFIG_UNKNOWN_RULE", f"未知规则: {key}")
        if key == "stability_drop_threshold":
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise InputError("CONFIG_RULE_INVALID", "stability_drop_threshold 必须是非负整数")
            rules[key] = value
            continue
        if not isinstance(value, str) or value not in SEVERITY_VALUES:
            raise InputError("CONFIG_RULE_INVALID", f"规则 {key} 的级别非法: {value}")
        if key == "default_on_change" and value == "pass":
            raise InputError("CONFIG_RULE_INVALID", "default_on_change 不能为 pass")
        rules[key] = value
    return rules


def load_config(path: str | Path, root: Path = PROJECT_ROOT) -> AppConfig:
    root = root.resolve()
    try:
        config_path = resolve_under_root(path, root, "config")
    except InputError:
        raise
    if not config_path.exists():
        raise InputError("CONFIG_NOT_FOUND", f"配置文件不存在: {path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputError("CONFIG_UNREADABLE", f"无法读取配置: {exc}") from exc
    data = _require_mapping(raw)
    unknown = sorted(set(data) - CONFIG_KEYS)
    if unknown:
        raise InputError("CONFIG_UNKNOWN_KEY", f"配置存在未知键: {', '.join(unknown)}")
    if data.get("schema_version") != 1:
        raise InputError("CONFIG_SCHEMA_VERSION", "仅支持 schema_version: 1")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise InputError("CONFIG_MISSING_NAME", "配置缺少非空 name")
    raw_recipes = data.get("recipes")
    if not isinstance(raw_recipes, list) or not raw_recipes:
        raise InputError("CONFIG_RECIPES_INVALID", "recipes 必须是非空列表")
    recipe_paths: list[Path] = []
    for item in raw_recipes:
        if not isinstance(item, str) or not item.strip():
            raise InputError("CONFIG_RECIPES_INVALID", "recipe 路径必须是非空字符串")
        recipe_paths.append(resolve_under_root(item, root, "recipe"))
    snapshot_dir = resolve_under_root(
        data.get("snapshot_dir", "fixtures/snapshots"), root, "snapshot_dir"
    )
    if not snapshot_dir.is_dir():
        raise InputError("CONFIG_SNAPSHOT_DIR", f"快照目录不存在: {snapshot_dir}")
    ledger_path = resolve_under_root(
        data.get("ledger_path", "var/selregress.db"), root, "ledger_path"
    )
    runs_dir = resolve_under_root(data.get("runs_dir", "runs"), root, "runs_dir")
    fixed_time = data.get("fixed_time")
    if fixed_time is not None and not isinstance(fixed_time, str):
        raise InputError("CONFIG_FIXED_TIME", "fixed_time 必须是字符串或 null")
    return AppConfig(
        root=root,
        config_path=config_path,
        name=name.strip(),
        snapshot_dir=snapshot_dir,
        recipe_paths=tuple(recipe_paths),
        ledger_path=ledger_path,
        runs_dir=runs_dir,
        fixed_time=fixed_time,
        rules=_validate_rules(data.get("rules")),
        description=str(data.get("description", "")),
    )


def load_config_for_planning(path: str | Path) -> AppConfig:
    """加载修复预演配置，允许使用项目根目录之外的隔离 fixture。

    常规 `run`/`demo` 仍要求输入位于项目根目录内。修复预演经常针对临时合成配方
    与快照运行，因此当配置文件不在项目根目录内时，将配置所在目录视为该次预演的
    根目录；配置内声明的快照、配方、基线与台账路径都必须位于该目录内。这样既不
    放宽常规命令的边界，也能让临时目录中的 fixture 完全隔离运行。
    """

    candidate = Path(path)
    resolved = candidate.resolve() if candidate.is_absolute() else (Path.cwd() / candidate).resolve()
    project_root = PROJECT_ROOT.resolve()
    try:
        resolved.relative_to(project_root)
        root = project_root
    except ValueError:
        root = resolved.parent
    return load_config(resolved, root)
