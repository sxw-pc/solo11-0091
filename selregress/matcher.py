"""CSS 选择器命中与多候选一致性判定。

`match_nodes` 保留发布行为：按声明顺序取第一个非空候选。修复预演需要在不改动
发布行为的前提下，对“候选选择器动作”做真实重跑，因此新增可选的 `selectors`
覆盖参数；修复引擎只通过该入口构造反事实，不会写回配方。
"""

from __future__ import annotations

from typing import Any, Sequence

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


def match_nodes(
    soup: Any,
    spec: FieldSpec,
    *,
    selectors: Sequence[str] | None = None,
) -> NodeSelection:
    """匹配节点。

    `selectors` 仅用于修复预演等内存场景：传入后会替代 `spec.selectors` 执行
    同一套“第一个非空候选”逻辑，便于对替换主选择器/追加 fallback 做反事实。
    默认行为与发布版本完全一致。
    """

    effective: tuple[str, ...]
    if selectors is None:
        effective = tuple(spec.selectors)
    else:
        effective = tuple(str(item) for item in selectors)
    hits: list[dict[str, Any]] = []
    selector_values: dict[str, list[str]] = {}
    chosen_nodes: list[Any] = []
    used_selector = ""
    for selector in effective:
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
    nonempty = [selector for selector in effective if selector_values.get(selector)]
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
        used_selector=used_selector or (effective[0] if effective else ""),
        ambiguous=ambiguous,
        ambiguity_detail=detail,
    )
