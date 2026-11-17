"""只读失效归因与候选选择器建议。最多五条，绝不写回配方。"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from .models import FieldSpec, NodeSelection
from .util import truncate

RISK = "仅作人工复核线索，未经核验不得自动写回配方。"
CLASS_RE = re.compile(r"\.([A-Za-z_][\w-]*)")
ID_RE = re.compile(r"#([A-Za-z_][\w-]*)")
TAG_RE = re.compile(r"(?:^|[\s>+~])([A-Za-z][\w-]*)")


def _selector_tokens(selector: str) -> dict[str, Any]:
    tags = [tag for tag in TAG_RE.findall(selector) if tag not in {"html", "body"}]
    return {
        "selector": selector,
        "tags": sorted(set(tags)),
        "classes": sorted(set(CLASS_RE.findall(selector))),
        "ids": sorted(set(ID_RE.findall(selector))),
    }


def _classes(node: Any) -> list[str]:
    value = node.get("class", [])
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value]


def _element_selector(node: Any) -> str:
    name = node.name or "*"
    classes = _classes(node)
    return f"{name}.{classes[0]}" if classes else name


def _path_chain(node: Any, limit: int = 5) -> list[str]:
    chain: list[str] = []
    current = node
    while current is not None and getattr(current, "name", None) and len(chain) < limit:
        chain.append(_element_selector(current))
        current = current.parent
    return list(reversed(chain))


def _unique_path(node: Any) -> str | None:
    parts: list[str] = []
    current = node
    depth = 0
    while current is not None and getattr(current, "name", None) and current.name != "[document]":
        name = current.name
        if name in {"html", "body"}:
            break
        part = name
        node_id = current.get("id")
        classes = _classes(current)
        if node_id:
            part += f"#{node_id}"
        elif classes:
            part += f".{classes[0]}"
        siblings = current.find_previous_siblings(name) + current.find_next_siblings(name)
        if siblings:
            index = 1
            for sibling in current.previous_siblings:
                if getattr(sibling, "name", None) == name:
                    index += 1
            part += f":nth-of-type({index})"
        parts.append(part)
        current = current.parent
        depth += 1
        if depth > 6:
            break
    if not parts:
        return None
    return " > ".join(reversed(parts))


def _nearby_text(node: Any) -> str:
    previous = node.find_previous(string=True)
    following = node.find_next(string=True)
    bits = [str(previous or "").strip(), str(node.get_text(" ", strip=True)), str(following or "").strip()]
    return truncate(" / ".join(item for item in bits if item), 160)


def _confidence(score: int) -> str:
    if score >= 85:
        return "high"
    if score >= 60:
        return "medium"
    return "low"


def _last_compound(selector: str) -> str:
    return re.split(r"[\s>+~]+", selector.strip())[-1]


def _class_suggestions(soup: Any, spec: FieldSpec) -> list[dict[str, Any]]:
    last_parts = [_last_compound(selector) for selector in spec.selectors]
    target_classes = sorted({item for part in last_parts for item in CLASS_RE.findall(part)})
    target_tags = sorted({item for part in last_parts for item in TAG_RE.findall(part)})
    candidates: dict[str, dict[str, Any]] = {}
    for node in soup.find_all(True):
        node_classes = _classes(node)
        if not node_classes:
            continue
        best_ratio = 0.0
        best_class = ""
        for source in target_classes:
            for existing in node_classes:
                ratio = SequenceMatcher(None, source.lower(), existing.lower()).ratio()
                if ratio > best_ratio:
                    best_ratio, best_class = ratio, existing
        if best_ratio < 0.45:
            continue
        tag_bonus = 10 if node.name in target_tags else 0
        score = min(95, int(round(best_ratio * 80)) + tag_bonus)
        selector = f"{node.name}.{best_class}"
        entry = {
            "selector": selector,
            "confidence": _confidence(score),
            "score": score,
            "reason": f"类名 {best_class!r} 与原选择器类名相似度较高",
            "risk": RISK,
            "_node": node,
        }
        if selector not in candidates or score > candidates[selector]["score"]:
            candidates[selector] = entry
    for entry in candidates.values():
        node = entry.pop("_node")
        entry["evidence"] = {
            "parent_chain": _path_chain(node),
            "nearby_text": _nearby_text(node),
        }
    return sorted(candidates.values(), key=lambda item: (-item["score"], item["selector"]))[:5]


def _multi_suggestions(
    soup: Any, selection: NodeSelection
) -> list[dict[str, Any]]:
    suggestions: dict[str, dict[str, Any]] = {}
    for index, node in enumerate(selection.nodes[:6]):
        path = _unique_path(node)
        if not path:
            continue
        try:
            count = len(soup.select(path))
        except Exception:
            count = 0
        if count == 0:
            continue
        score = 88 if count == 1 else 60
        if index == 0:
            score += 2
        suggestions[path] = {
            "selector": path,
            "confidence": _confidence(score),
            "score": score,
            "reason": (
                "路径可将命中收窄到单节点，但需确认业务是否真有多个合法值"
                if count == 1 else "路径减少命中数，仍需人工确认字段语义"
            ),
            "risk": RISK,
            "evidence": {
                "parent_chain": _path_chain(node),
                "nearby_text": _nearby_text(node),
            },
        }
    return sorted(suggestions.values(), key=lambda item: (-item["score"], item["selector"]))[:5]


def analyze_failure(
    soup: Any, spec: FieldSpec, selection: NodeSelection
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tokens = [_selector_tokens(selector) for selector in spec.selectors]
    evidence: dict[str, Any] = {
        "original_selectors": tokens,
        "match_count": selection.count,
        "selector_hits": selection.selector_hits,
        "ambiguity_detail": selection.ambiguity_detail,
    }
    if selection.nodes:
        evidence["parent_chain"] = _path_chain(selection.nodes[0])
        evidence["nearby_text"] = _nearby_text(selection.nodes[0])
        suggestions = _multi_suggestions(soup, selection)
    else:
        evidence["parent_chain"] = []
        evidence["nearby_text"] = ""
        suggestions = _class_suggestions(soup, spec)
    if suggestions:
        evidence["suggestion_evidence"] = [
            item.get("evidence", {}) for item in suggestions
        ]
    for item in suggestions:
        item.pop("evidence", None)
    evidence["advice"] = "建议由维护人员核对 DOM 后更新配方；本工具不会改写文件。"
    return evidence, suggestions


def _candidate_entry(
    *,
    selector: str,
    score: int,
    source: str,
    reason: str,
    evidence: dict[str, Any] | None = None,
    derived_from: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "selector": selector,
        "confidence": _confidence(score),
        "score": score,
        "source": source,
        "reason": reason,
        "risk": RISK,
    }
    if evidence:
        entry["evidence"] = evidence
    if derived_from:
        entry["derived_from"] = derived_from
    return entry


def enumerate_repair_candidates(
    soup: Any,
    spec: FieldSpec,
    selection: NodeSelection,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """枚举可供修复预演真实重跑的候选选择器。

    与 `analyze_failure` 一样只读、确定性，但返回可供 matcher/extractor 重跑的
    选择器，而不是只给出置信度。这里仍然是“候选线索”：是否真正可交付必须由
    调用方在全部目标快照上重新抽取后判定，不能直接采信 score/confidence。

    来源包括：
    - 类名相似节点（同时给出 `tag.class` 与更通用的 `.class` 形式）；
    - 多命中节点收窄后的唯一 DOM 路径。
    """

    found: dict[str, dict[str, Any]] = {}

    def add(entry: dict[str, Any]) -> None:
        selector = str(entry.get("selector", "")).strip()
        if not selector:
            return
        entry = dict(entry)
        entry["selector"] = selector
        existing = found.get(selector)
        if existing is None or (
            entry["score"], entry.get("source", "")
        ) > (existing["score"], existing.get("source", "")):
            found[selector] = entry

    for item in _class_suggestions(soup, spec):
        add(item)
        for class_name in CLASS_RE.findall(item["selector"]):
            add(
                _candidate_entry(
                    selector=f".{class_name}",
                    score=int(item["score"]),
                    source="class_similarity",
                    reason=f"由相似类名候选 {item['selector']} 派生的类选择器",
                    evidence=item.get("evidence"),
                    derived_from=item["selector"],
                )
            )
    if selection.count > 1:
        for item in _multi_suggestions(soup, selection):
            add(
                _candidate_entry(
                    selector=item["selector"],
                    score=int(item["score"]),
                    source="dom_path",
                    reason=str(item.get("reason", "唯一 DOM 路径候选")),
                    evidence=item.get("evidence"),
                )
            )
    ordered = sorted(
        found.values(),
        key=lambda entry: (-int(entry["score"]), entry["selector"], entry.get("source", "")),
    )
    return ordered[: max(1, int(limit))]
