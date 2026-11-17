"""字段取值、归一化与类型转换。解析错误转为结构化状态，不向外抛异常。"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from .models import Extraction, FieldSpec, NodeSelection
from .util import truncate

TRUE_VALUES = {"true", "1", "yes", "y", "on", "是", "有", "真"}
FALSE_VALUES = {"false", "0", "no", "n", "off", "否", "无", "假"}
DATE_FORMATS = (
    "%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日", "%Y%m%d",
    "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S",
)
INTEGER_RE = re.compile(r"^[+-]?\d+$")
DECIMAL_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")


def _raw_value(node: Any, spec: FieldSpec) -> str:
    if spec.extract == "text":
        return node.get_text(" ", strip=True)
    if spec.extract == "html":
        return node.decode_contents(formatter="minimal")
    key = spec.extract
    if spec.extract == "attr":
        key = spec.attribute or ""
    value = node.get(key) if key else None
    return "" if value is None else str(value)


def _try_iso_date(value: str) -> str | None:
    text = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_text(value: str, rules: tuple[Any, ...]) -> tuple[str, str | None]:
    result = value
    for rule in rules:
        try:
            if isinstance(rule, str):
                if rule == "trim":
                    result = result.strip()
                elif rule == "collapse_ws":
                    result = " ".join(result.split())
                elif rule == "lower":
                    result = result.lower()
                elif rule == "upper":
                    result = result.upper()
                elif rule == "strip_thousands":
                    result = result.replace(",", "").replace("，", "")
                elif rule == "nfkc":
                    result = unicodedata.normalize("NFKC", result)
                elif rule == "iso_date":
                    iso = _try_iso_date(result)
                    if iso is not None:
                        result = iso
                else:
                    return result, f"未知归一化规则: {rule}"
            elif isinstance(rule, dict) and rule.get("type") == "regex_replace":
                result = re.sub(rule["pattern"], rule["replacement"], result)
        except (re.error, KeyError, TypeError) as exc:
            return result, f"归一化失败: {exc}"
    return result, None


def _numeric_text(value: str) -> str:
    cleaned = value.replace(",", "").replace("，", "").strip()
    cleaned = re.sub(r"^(?:CNY|RMB|人民币|¥|￥|\$)\s*", "", cleaned, flags=re.I)
    cleaned = re.sub(r"\s*(?:元|人民币)$", "", cleaned)
    return cleaned


def convert_value(value: str, field_type: str) -> tuple[str, str | None]:
    text = value
    if field_type == "string":
        return text, None
    if field_type == "integer":
        cleaned = _numeric_text(text)
        if not INTEGER_RE.fullmatch(cleaned):
            return text, f"无法转换为 integer: {truncate(text, 60)}"
        return str(int(cleaned)), None
    if field_type == "decimal":
        cleaned = _numeric_text(text)
        if not DECIMAL_RE.fullmatch(cleaned):
            return text, f"无法转换为 decimal: {truncate(text, 60)}"
        try:
            number = Decimal(cleaned)
        except InvalidOperation:
            return text, f"无法转换为 decimal: {truncate(text, 60)}"
        if number == 0:
            return "0", None
        return format(number.normalize(), "f"), None
    if field_type == "date":
        iso = _try_iso_date(text)
        if iso is None:
            return text, f"无法转换为 date: {truncate(text, 60)}"
        return iso, None
    if field_type == "boolean":
        key = text.strip().lower()
        if key in TRUE_VALUES:
            return "true", None
        if key in FALSE_VALUES:
            return "false", None
        return text, f"无法转换为 boolean: {truncate(text, 60)}"
    if field_type == "url":
        candidate = text.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            return candidate, None
        if candidate.startswith(("/", "./", "../", "#")):
            return candidate, None
        return text, f"无法转换为 url: {truncate(text, 60)}"
    return text, f"未知字段类型: {field_type}"


def extract_field(selection: NodeSelection, spec: FieldSpec) -> Extraction:
    raw_values: list[str] = []
    normalized_values: list[str] = []
    errors: list[str] = []
    nodes = selection.nodes
    if not nodes:
        return Extraction(
            raw_values=[], normalized_values=[], normalized_value=None, match_count=0,
            conversion_status="no_match", used_selector=selection.used_selector,
            selector_hits=selection.selector_hits, ambiguous=selection.ambiguous,
            ambiguity_detail=selection.ambiguity_detail,
        )
    for node in nodes:
        try:
            raw = _raw_value(node, spec)
        except Exception as exc:  # 单个节点异常不应阻断其他字段
            raw_values.append("")
            errors.append(f"取值失败: {exc}")
            continue
        raw_values.append(raw)
    convert_count = len(raw_values) if spec.multi else 1
    for raw in raw_values[:convert_count]:
        normalized, norm_error = normalize_text(raw, spec.normalize)
        if norm_error:
            errors.append(norm_error)
        if normalized.strip() == "":
            normalized_values.append("")
            continue
        converted, conversion_error = convert_value(normalized, spec.field_type)
        if conversion_error:
            errors.append(conversion_error)
        normalized_values.append(converted)
    nonempty = [item for item in normalized_values if item != ""]
    if spec.multi:
        normalized_value: Any = spec.separator.join(nonempty) if spec.separator else list(nonempty)
    else:
        normalized_value = normalized_values[0] if normalized_values else ""
    if not nonempty:
        status = "empty"
    elif errors:
        status = "type_error"
    else:
        status = "ok"
    error = "; ".join(dict.fromkeys(errors)) if errors else None
    return Extraction(
        raw_values=raw_values,
        normalized_values=normalized_values,
        normalized_value=normalized_value,
        match_count=len(nodes),
        conversion_status=status,
        used_selector=selection.used_selector,
        selector_hits=selection.selector_hits,
        ambiguous=selection.ambiguous,
        ambiguity_detail=selection.ambiguity_detail,
        error=error,
    )


def extraction_failure_kind(extraction: Extraction) -> str | None:
    """把抽取结果归类为稳定的失败类型，供诊断与修复预演共用。

    只看 `extract_field` 的真实结果，不接受候选建议分数之类的旁证。
    `ambiguous_selector` 由匹配层给出，不在此处归类。
    """

    if extraction.match_count == 0 or extraction.conversion_status == "no_match":
        return "no_match"
    if extraction.conversion_status == "empty":
        return "empty_value"
    if extraction.conversion_status == "type_error":
        return "type_error"
    return None
