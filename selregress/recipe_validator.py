"""采集配方静态校验与加载。错误码保持稳定并记录在文档中。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import soupsieve
import yaml

from .models import FieldSpec, Recipe
from .util import InputError, PROJECT_ROOT, relative_posix, resolve_under_root, sha256_file

RECIPE_KEYS = {
    "schema_version", "name", "site", "page_type", "description", "fields", "baseline",
}
FIELD_KEYS = {
    "name", "selector", "selectors", "type", "required", "extract", "attribute",
    "normalize", "multi", "separator", "on_change", "notes",
}
FIELD_TYPES = {"string", "integer", "decimal", "date", "url", "boolean"}
EXTRACT_MODES = {"text", "attr", "href", "src", "html"}
ON_CHANGE_VALUES = {"ignore", "warn", "fail"}
TEXT_NORMALIZERS = {
    "trim", "collapse_ws", "lower", "upper", "strip_thousands", "nfkc", "iso_date",
}


@dataclass(slots=True)
class RecipeIssue:
    code: str
    path: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "path": self.path, "message": self.message}


class RecipeValidationError(InputError):
    def __init__(self, source: str, issues: list[RecipeIssue]):
        self.issues = issues
        summary = "; ".join(f"{item.code}@{item.path}" for item in issues[:4])
        super().__init__(
            "RECIPE_INVALID",
            f"配方校验失败 ({source}): {summary}",
            {"source": source, "issues": [item.to_dict() for item in issues]},
        )


def _issue(issues: list[RecipeIssue], code: str, path: str, message: str) -> None:
    issues.append(RecipeIssue(code=code, path=path, message=message))


def _validate_normalizers(value: Any, path: str, issues: list[RecipeIssue]) -> list[Any]:
    if value is None:
        return []
    if not isinstance(value, list):
        _issue(issues, "R011_INVALID_NORMALIZE", path, "normalize 必须是列表")
        return []
    output: list[Any] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if isinstance(item, str):
            if item not in TEXT_NORMALIZERS:
                _issue(issues, "R011_INVALID_NORMALIZE", item_path, f"未知归一化规则: {item}")
            else:
                output.append(item)
            continue
        if isinstance(item, dict) and set(item) == {"type", "pattern", "replacement"}:
            if item.get("type") != "regex_replace":
                _issue(issues, "R011_INVALID_NORMALIZE", item_path, "映射规则 type 必须为 regex_replace")
            elif not isinstance(item.get("pattern"), str) or not isinstance(item.get("replacement"), str):
                _issue(issues, "R011_INVALID_NORMALIZE", item_path, "正则规则需要字符串 pattern 与 replacement")
            else:
                try:
                    re.compile(item["pattern"])
                except re.error as exc:
                    _issue(issues, "R011_INVALID_NORMALIZE", item_path, f"正则表达式非法: {exc}")
                else:
                    output.append(dict(item))
            continue
        _issue(issues, "R011_INVALID_NORMALIZE", item_path, "归一化规则格式非法")
    return output


def _validate_field(raw: Any, index: int, issues: list[RecipeIssue]) -> FieldSpec | None:
    base = f"fields[{index}]"
    if not isinstance(raw, dict):
        _issue(issues, "R004_FIELD_INVALID", base, "字段必须是映射")
        return None
    for key in sorted(set(raw) - FIELD_KEYS):
        _issue(issues, "R005_UNKNOWN_FIELD_KEY", f"{base}.{key}", f"字段存在未知键: {key}")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        _issue(issues, "R004_FIELD_MISSING_KEY", f"{base}.name", "字段缺少非空 name")
        name = ""
    selectors_raw = raw.get("selectors")
    selector_raw = raw.get("selector")
    if selectors_raw is not None and selector_raw is not None:
        _issue(issues, "R008_SELECTOR_CONFLICT", base, "selector 与 selectors 不能同时声明")
    if selectors_raw is not None:
        if not isinstance(selectors_raw, list):
            _issue(issues, "R008_EMPTY_SELECTOR", f"{base}.selectors", "selectors 必须是列表")
            selectors_raw = []
        selectors = selectors_raw
    elif selector_raw is not None:
        selectors = [selector_raw]
    else:
        selectors = []
    valid_selectors: list[str] = []
    for s_index, selector in enumerate(selectors):
        s_path = f"{base}.selectors[{s_index}]"
        if not isinstance(selector, str) or not selector.strip():
            _issue(issues, "R008_EMPTY_SELECTOR", s_path, "选择器不能为空")
            continue
        try:
            soupsieve.compile(selector)
        except Exception as exc:  # soupsieve 的异常类型随版本变化
            _issue(issues, "R009_INVALID_SELECTOR", s_path, f"CSS 选择器非法: {exc}")
            continue
        valid_selectors.append(selector.strip())
    if not valid_selectors:
        _issue(issues, "R008_EMPTY_SELECTOR", base, "字段至少需要一个有效选择器")
    field_type = raw.get("type", "string")
    if field_type not in FIELD_TYPES:
        _issue(issues, "R007_INVALID_FIELD_TYPE", f"{base}.type", f"非法字段类型: {field_type}")
        field_type = "string"
    required = raw.get("required", True)
    if not isinstance(required, bool):
        _issue(issues, "R015_INVALID_REQUIRED", f"{base}.required", "required 必须是布尔值")
        required = True
    extract = raw.get("extract", "text")
    if extract not in EXTRACT_MODES:
        _issue(issues, "R010_INVALID_EXTRACT", f"{base}.extract", f"非法取值方式: {extract}")
        extract = "text"
    attribute = raw.get("attribute")
    if extract == "attr" and (not isinstance(attribute, str) or not attribute.strip()):
        _issue(issues, "R010_ATTRIBUTE_REQUIRED", f"{base}.attribute", "extract: attr 需要 attribute")
    multi = raw.get("multi", False)
    if not isinstance(multi, bool):
        _issue(issues, "R014_INVALID_MULTI", f"{base}.multi", "multi 必须是布尔值")
        multi = False
    separator = raw.get("separator")
    if separator is not None and (not isinstance(separator, str) or not separator):
        _issue(issues, "R014_INVALID_SEPARATOR", f"{base}.separator", "separator 必须是非空字符串")
    if separator is not None and not multi:
        _issue(issues, "R014_SEPARATOR_WITHOUT_MULTI", f"{base}.separator", "separator 仅可用于 multi 字段")
    on_change = raw.get("on_change", "warn")
    if on_change not in ON_CHANGE_VALUES:
        _issue(issues, "R017_INVALID_ON_CHANGE", f"{base}.on_change", f"非法变更级别: {on_change}")
        on_change = "warn"
    normalize = _validate_normalizers(raw.get("normalize"), f"{base}.normalize", issues)
    return FieldSpec(
        name=name.strip(),
        selectors=tuple(valid_selectors),
        field_type=field_type,
        required=required,
        extract=extract,
        attribute=attribute.strip() if isinstance(attribute, str) and attribute.strip() else None,
        normalize=tuple(normalize),
        multi=multi,
        separator=separator if isinstance(separator, str) and separator else None,
        on_change=on_change,
    )


def validate_recipe_data(
    data: Any, *, source: str = "<memory>", root: Path = PROJECT_ROOT
) -> tuple[Recipe | None, list[RecipeIssue]]:
    issues: list[RecipeIssue] = []
    if not isinstance(data, dict):
        _issue(issues, "R001_NOT_MAPPING", "$", "配方根节点必须是映射")
        return None, issues
    for key in sorted(set(data) - RECIPE_KEYS):
        _issue(issues, "R002_UNKNOWN_KEY", key, f"配方存在未知键: {key}")
    if data.get("schema_version") != 1:
        _issue(issues, "R016_SCHEMA_VERSION", "schema_version", "仅支持 schema_version: 1")
    name = data.get("name")
    site = data.get("site")
    page_type = data.get("page_type")
    if not isinstance(name, str) or not name.strip():
        _issue(issues, "R003_MISSING_KEY", "name", "缺少非空 name")
        name = ""
    if not isinstance(site, str) or not site.strip():
        _issue(issues, "R003_MISSING_KEY", "site", "缺少非空 site")
        site = ""
    if page_type not in {"list", "detail"}:
        _issue(issues, "R007_INVALID_PAGE_TYPE", "page_type", "page_type 必须是 list 或 detail")
        page_type = "detail"
    raw_fields = data.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        _issue(issues, "R003_MISSING_KEY", "fields", "fields 必须是非空列表")
        raw_fields = []
    fields: list[FieldSpec] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_fields):
        spec = _validate_field(raw, index, issues)
        if spec is None:
            continue
        if spec.name in seen:
            _issue(issues, "R006_DUPLICATE_FIELD", f"fields[{index}].name", f"字段名重复: {spec.name}")
        seen.add(spec.name)
        if spec.name:
            fields.append(spec)
    baseline = data.get("baseline")
    baseline_rel: str | None = None
    if baseline is not None:
        if not isinstance(baseline, str) or not baseline.strip():
            _issue(issues, "R012_BASELINE_INVALID", "baseline", "baseline 必须是非空路径")
        else:
            try:
                baseline_path = resolve_under_root(baseline, root, "baseline")
                if not baseline_path.is_file():
                    _issue(issues, "R012_BASELINE_NOT_FOUND", "baseline", f"基线文件不存在: {baseline}")
                else:
                    baseline_rel = relative_posix(baseline_path, root)
            except InputError as exc:
                _issue(issues, "R013_BASELINE_OUTSIDE_ROOT", "baseline", exc.message)
    if issues:
        return None, issues
    path = resolve_under_root(source, root, "recipe") if source != "<memory>" else Path(source)
    digest = sha256_file(path) if path.is_file() else ""
    recipe = Recipe(
        name=name.strip(), site=site.strip(), page_type=page_type,
        fields=tuple(fields), path=source, sha256=digest, baseline=baseline_rel,
        schema_version=1, description=str(data.get("description", "")),
    )
    return recipe, issues


def load_recipe(path: str | Path, root: Path = PROJECT_ROOT) -> Recipe:
    root = root.resolve()
    recipe_path = resolve_under_root(path, root, "recipe")
    if not recipe_path.is_file():
        raise InputError("RECIPE_NOT_FOUND", f"配方文件不存在: {path}")
    try:
        data = yaml.safe_load(recipe_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InputError("RECIPE_UNREADABLE", f"无法读取配方: {exc}") from exc
    rel = relative_posix(recipe_path, root)
    recipe, issues = validate_recipe_data(data, source=rel, root=root)
    if issues:
        raise RecipeValidationError(rel, issues)
    if recipe is None:
        raise InputError("RECIPE_INVALID", "配方为空")
    recipe.path = rel
    recipe.sha256 = sha256_file(recipe_path)
    return recipe
