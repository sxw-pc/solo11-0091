"""CSS 选择器命中与多候选一致性判定。"""

from __future__ import annotations

from typing import Any

from .models import FieldSpec, NodeSelection
from .util import truncate


def _node_signature(node: Any, spec: FieldSpec) -> str:
    if spec.extract in {"attr", "href", "src"}:
        key = spec.attribute if spec.extract == "attr" else spec.extract
        value = node.get(key) if key else None
        return str(value or "").strip()
    if spec.extract == "html":
        return " ".join(node.decode_contents().split())
    return " ".join(node.get_text(" ", strip=True).split())


def match_nodes(soup: Any, spec: FieldSpec) -> NodeSelection:
    hits: list[dict[str, Any]] = []
    selector_values: dict[str, list[str]] = {}
    chosen_nodes: list[Any] = []
    used_selector = ""
    for selector in spec.selectors:
        try:
            nodes = soup.select(selector)
        except Exception:
            nodes = []
        signatures = [_node_signature(node, spec) for node in nodes]
        selector_values[selector] = signatures
        hits.append(
            {
                "selector": selector,
                "count": len(nodes),
                "samples": [truncate(item, 80) for item in signatures[:3]],
            }
        )
        if nodes and not used_selector:
            used_selector = selector
            chosen_nodes = nodes
    nonempty = [selector for selector in spec.selectors if selector_values.get(selector)]
    distinct = sorted({tuple(selector_values[selector]) for selector in nonempty})
    ambiguous = len(nonempty) > 1 and len(distinct) > 1
    detail = ""
    if ambiguous:
        pairs = ", ".join(
            f"{selector}={len(selector_values[selector])}" for selector in nonempty
        )
        detail = f"多个候选选择器命中结果不一致: {pairs}"
    return NodeSelection(
        nodes=chosen_nodes,
        selector_hits=hits,
        used_selector=used_selector or (spec.selectors[0] if spec.selectors else ""),
        ambiguous=ambiguous,
        ambiguity_detail=detail,
    )
