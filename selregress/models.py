"""领域模型。所有模型均只保存可序列化值，不持有 BeautifulSoup 节点。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class FieldSpec:
    name: str
    selectors: tuple[str, ...]
    field_type: str = "string"
    required: bool = True
    extract: str = "text"
    attribute: str | None = None
    normalize: tuple[Any, ...] = ()
    multi: bool = False
    separator: str | None = None
    on_change: str = "warn"

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "selectors": list(self.selectors),
            "type": self.field_type,
            "required": self.required,
            "extract": self.extract,
            "attribute": self.attribute,
            "normalize": list(self.normalize),
            "multi": self.multi,
            "separator": self.separator,
            "on_change": self.on_change,
        }


@dataclass(slots=True)
class Recipe:
    name: str
    site: str
    page_type: str
    fields: tuple[FieldSpec, ...]
    path: str
    sha256: str
    baseline: str | None = None
    schema_version: int = 1
    description: str = ""

    def to_meta(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "site": self.site,
            "page_type": self.page_type,
            "path": self.path,
            "sha256": self.sha256,
            "baseline": self.baseline,
            "schema_version": self.schema_version,
            "field_count": len(self.fields),
            "fields": [item.to_meta() for item in self.fields],
        }


@dataclass(slots=True)
class SnapshotMeta:
    site: str
    page_type: str
    version: str
    path: str
    sha256: str
    size: int
    captured_at: str = ""
    warnings: tuple[str, ...] = ()

    def to_meta(self) -> dict[str, Any]:
        return {
            "site": self.site,
            "page_type": self.page_type,
            "version": self.version,
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "captured_at": self.captured_at,
            "warnings": list(self.warnings),
        }


@dataclass(slots=True)
class NodeSelection:
    nodes: list[Any] = field(default_factory=list)
    selector_hits: list[dict[str, Any]] = field(default_factory=list)
    used_selector: str = ""
    ambiguous: bool = False
    ambiguity_detail: str = ""

    @property
    def count(self) -> int:
        return len(self.nodes)


@dataclass(slots=True)
class Extraction:
    raw_values: list[str]
    normalized_values: list[str]
    normalized_value: Any
    match_count: int
    conversion_status: str
    used_selector: str
    selector_hits: list[dict[str, Any]]
    ambiguous: bool = False
    ambiguity_detail: str = ""
    error: str | None = None

    @property
    def has_value(self) -> bool:
        return bool(self.normalized_values) and any(item != "" for item in self.normalized_values)


@dataclass(slots=True)
class Finding:
    code: str
    severity: str
    message: str
    recipe_name: str
    site: str
    page_type: str
    version: str
    snapshot_path: str
    field_name: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    suggestions: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "recipe_name": self.recipe_name,
            "site": self.site,
            "page_type": self.page_type,
            "version": self.version,
            "snapshot_path": self.snapshot_path,
            "field_name": self.field_name,
            "evidence": self.evidence,
            "suggestions": self.suggestions,
        }


@dataclass(slots=True)
class BaselineDiff:
    change_type: str
    severity: str
    recipe_name: str
    site: str
    page_type: str
    field_name: str
    old_value: Any
    new_value: Any
    message: str
    snapshot_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "change_type": self.change_type,
            "severity": self.severity,
            "recipe_name": self.recipe_name,
            "site": self.site,
            "page_type": self.page_type,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "message": self.message,
            "snapshot_path": self.snapshot_path,
        }


@dataclass(slots=True)
class FieldOutcome:
    recipe_name: str
    recipe_path: str
    site: str
    page_type: str
    version: str
    snapshot_path: str
    snapshot_sha256: str
    spec: FieldSpec
    extraction: Extraction
    flags: list[str] = field(default_factory=list)
    score: int = 100
    score_reasons: list[str] = field(default_factory=list)
    status: str = "pass"

    @property
    def field_name(self) -> str:
        return self.spec.name

    @property
    def sort_key(self) -> tuple[Any, ...]:
        return (
            self.recipe_name,
            self.site,
            self.page_type,
            self.version,
            self.snapshot_path,
            self.spec.name,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "recipe_name": self.recipe_name,
            "recipe_path": self.recipe_path,
            "site": self.site,
            "page_type": self.page_type,
            "version": self.version,
            "snapshot_path": self.snapshot_path,
            "snapshot_sha256": self.snapshot_sha256,
            "field_name": self.spec.name,
            "field_type": self.spec.field_type,
            "required": self.spec.required,
            "multi": self.spec.multi,
            "selectors": list(self.spec.selectors),
            "used_selector": self.extraction.used_selector,
            "selector_hits": self.extraction.selector_hits,
            "raw_values": self.extraction.raw_values,
            "normalized_value": self.extraction.normalized_value,
            "normalized_values": self.extraction.normalized_values,
            "match_count": self.extraction.match_count,
            "conversion_status": self.extraction.conversion_status,
            "has_value": self.extraction.has_value,
            "error": self.extraction.error,
            "ambiguous": self.extraction.ambiguous,
            "flags": sorted(set(self.flags)),
            "score": self.score,
            "score_reasons": self.score_reasons,
            "status": self.status,
        }


@dataclass(slots=True)
class RunResult:
    run_id: str
    status: str
    generated_at: str
    fixed_time: bool
    config_path: str
    fail_on_warn: bool
    recipes: list[dict[str, Any]]
    snapshots: list[dict[str, Any]]
    outcomes: list[FieldOutcome]
    findings: list[Finding]
    diffs: list[BaselineDiff]
    summary: dict[str, Any]
    report_dir: str = ""
    report_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return exit_code_for(self.status, self.fail_on_warn)


def exit_code_for(status: str, fail_on_warn: bool = False) -> int:
    if status == "pass":
        return 0
    if status == "warn":
        return 1 if fail_on_warn else 2
    return 1
