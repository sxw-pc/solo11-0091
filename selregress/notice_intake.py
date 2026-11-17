"""Offline list/detail intake for locally archived procurement notices."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .extractor import extract_field
from .matcher import match_nodes
from .models import FieldSpec
from .util import PROJECT_ROOT, InputError, relative_posix, resolve_under_root, sha256_bytes


_TITLE = FieldSpec("title", (".notice-title",), required=True)
_LINK = FieldSpec("link", ("a.notice-link",), field_type="url", extract="href", required=True)
_BUDGET = FieldSpec("budget", (".notice-budget",), field_type="decimal", required=True)
_DEADLINE = FieldSpec("deadline", ("time.deadline",), field_type="date", required=True)


def _load_html(path: str, root: Path) -> tuple[BeautifulSoup, str, str]:
    target = resolve_under_root(path, root, "notice snapshot")
    if not target.is_file():
        raise InputError("NOTICE_SNAPSHOT_MISSING", f"公告快照不存在: {path}")
    raw = target.read_bytes()
    if not raw:
        raise InputError("NOTICE_SNAPSHOT_EMPTY", f"公告快照为空: {path}")
    try:
        soup = BeautifulSoup(raw.decode("utf-8"), "html.parser")
    except UnicodeDecodeError as exc:
        raise InputError("NOTICE_SNAPSHOT_ENCODING", f"公告快照不是UTF-8: {path}") from exc
    return soup, relative_posix(target, root), sha256_bytes(raw)


def _value(scope: Any, spec: FieldSpec) -> tuple[Any, str]:
    selection = match_nodes(scope, spec)
    extraction = extract_field(selection, spec)
    if selection.count != 1 or selection.ambiguous or extraction.conversion_status != "ok":
        return None, extraction.conversion_status if selection.count == 1 else "match_count"
    return extraction.normalized_value, "ok"


def intake_notices(manifest_path: str | Path, *, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """Pair each list card with the explicitly declared local detail snapshot."""
    root = root.resolve()
    target = resolve_under_root(manifest_path, root, "notice manifest")
    try:
        raw = target.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError("NOTICE_MANIFEST_INVALID", "公告清单不是有效UTF-8 JSON") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != 1
            or not isinstance(manifest.get("list_snapshot"), str)
            or not isinstance(manifest.get("details"), list)):
        raise InputError("NOTICE_MANIFEST_INVALID", "公告清单须包含list_snapshot与details")
    declared: dict[str, str] = {}
    for item in manifest["details"]:
        if (not isinstance(item, dict) or set(item) != {"notice_id", "path"}
                or not all(isinstance(item[key], str) and item[key] for key in item)):
            raise InputError("NOTICE_MANIFEST_INVALID", "详情声明须有notice_id与path")
        if item["notice_id"] in declared:
            raise InputError("NOTICE_ID_DUPLICATE", "详情清单含重复公告ID")
        declared[item["notice_id"]] = item["path"]
    list_soup, list_path, list_sha = _load_html(manifest["list_snapshot"], root)
    cards = list_soup.select(".notice-card[data-notice-id]")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        notice_id = str(card.get("data-notice-id", "")).strip()
        if notice_id in seen:
            raise InputError("NOTICE_ID_DUPLICATE", f"列表含重复公告ID: {notice_id}")
        seen.add(notice_id)
        title, title_status = _value(card, _TITLE)
        link, link_status = _value(card, _LINK)
        budget, budget_status = _value(card, _BUDGET)
        summary: dict[str, Any] = {
            "notice_id": notice_id,
            "list": {"path": list_path, "sha256": list_sha, "title": title,
                     "link": link, "budget": budget},
            "detail": None,
        }
        detail_path = declared.get(notice_id)
        if not detail_path:
            summary.update(status="missing_detail", reasons=["missing_detail"])
            results.append(summary)
            continue
        detail_soup, detail_relpath, detail_sha = _load_html(detail_path, root)
        details = detail_soup.select(".notice-detail[data-notice-id]")
        reasons = [
            f"list_{name}_{status}" for name, status in (
                ("title", title_status), ("link", link_status), ("budget", budget_status)
            ) if status != "ok"
        ]
        if len(details) != 1 or details[0].get("data-notice-id") != notice_id:
            reasons.append("detail_id_mismatch")
            detail = details[0] if details else detail_soup
        else:
            detail = details[0]
        detail_title, title_state = _value(detail, _TITLE)
        detail_budget, budget_state = _value(detail, _BUDGET)
        deadline, deadline_state = _value(detail, _DEADLINE)
        for name, status in (("title", title_state), ("budget", budget_state),
                             ("deadline", deadline_state)):
            if status != "ok":
                reasons.append(f"detail_{name}_{status}")
        if title is not None and detail_title is not None and title != detail_title:
            reasons.append("title_disagrees")
        if budget is not None and detail_budget is not None and budget != detail_budget:
            reasons.append("budget_disagrees")
        canonical = str(detail.get("data-canonical-path", ""))
        if link is not None and link != canonical:
            reasons.append("link_disagrees")
        summary["detail"] = {
            "path": detail_relpath, "sha256": detail_sha,
            "title": detail_title, "budget": detail_budget, "deadline": deadline,
            "revision": str(detail.get("data-revision", "")),
        }
        summary.update(status="coherent" if not reasons else "mismatch",
                       reasons=sorted(set(reasons)))
        results.append(summary)
    results.sort(key=lambda item: item["notice_id"])
    return {
        "schema_version": 1,
        "manifest": {"path": relative_posix(target, root), "sha256": sha256_bytes(raw)},
        "status": "ready" if results and all(item["status"] == "coherent" for item in results) else "blocked",
        "notices": results,
    }
