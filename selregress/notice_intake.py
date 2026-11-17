"""Offline list/detail intake for locally archived procurement notices.

还包含只读的复核席位行动规划：在不联网、不发送投标、不修改快照的前提下，
把 intake 核出的公告材料（预算、截止日、详情 SHA、一致性）与本地复核清单
（技能、预算权限、按日席位、绑定详情 SHA 的转派授权）联合求解，给出每条
公告在截止前能否获得有效复核席位的确定性结论。可转派性只能来自显式的、
绑定当前详情 SHA 的委托记录，不能从姓名、预算数值相近或布尔字段推断。
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation
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


# ---------------------------------------------------------------------------
# 复核席位行动规划（只读，不联网、不发送投标、不修改快照）
# ---------------------------------------------------------------------------

REVIEW_CONTRACTS = ("designated_only", "delegation_allowed")
# 离线联合规划的规模上限：席位容量极小，超出该规模时深度优先搜索失去实际意义。
_MAX_PLAN_NOTICES = 24


def _roster_invalid(message: str) -> InputError:
    return InputError("REVIEW_ROSTER_INVALID", message)


def _is_sha256(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(char in "0123456789abcdef" for char in value))


def _parse_budget_limit(value: Any) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise _roster_invalid("预算权限须为十进制字符串或整数")
    try:
        limit = Decimal(str(value).strip())
    except InvalidOperation as exc:
        raise _roster_invalid(f"预算权限无法解析: {value}") from exc
    if not limit.is_finite() or limit < 0:
        raise _roster_invalid(f"预算权限须为有限非负数: {value}")
    return limit


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _load_roster(roster_path: str | Path, root: Path) -> tuple[dict[str, Any], dict[str, str]]:
    """读取并校验本地复核清单；不合法结构一律转为 InputError。"""
    target = resolve_under_root(roster_path, root, "review roster")
    if not target.is_file():
        raise InputError("REVIEW_ROSTER_MISSING", f"复核清单不存在: {roster_path}")
    try:
        raw = target.read_bytes()
        roster = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError("REVIEW_ROSTER_INVALID", "复核清单不是有效UTF-8 JSON") from exc
    if not isinstance(roster, dict) or roster.get("schema_version") != 1:
        raise _roster_invalid("复核清单须声明schema_version=1")
    notices_raw = roster.get("notices")
    reviewers_raw = roster.get("reviewers")
    delegations_raw = roster.get("delegations")
    if not (isinstance(notices_raw, list) and isinstance(reviewers_raw, list)
            and isinstance(delegations_raw, list)):
        raise _roster_invalid("复核清单须包含notices、reviewers与delegations")

    reviewers: dict[str, dict[str, Any]] = {}
    for item in reviewers_raw:
        if not isinstance(item, dict) or set(item) != {"reviewer_id", "skills", "budget_limit", "seats"}:
            raise _roster_invalid("复核员须含reviewer_id、skills、budget_limit与seats")
        reviewer_id = item["reviewer_id"]
        if not _nonempty_str(reviewer_id):
            raise _roster_invalid("复核员ID须为非空字符串")
        if reviewer_id in reviewers:
            raise _roster_invalid(f"复核清单含重复复核员: {reviewer_id}")
        skills = item["skills"]
        if (not isinstance(skills, list) or not skills
                or any(not _nonempty_str(skill) for skill in skills)
                or len(set(skills)) != len(skills)):
            raise _roster_invalid(f"复核员{reviewer_id}的技能须为去重的非空字符串列表")
        seats_raw = item["seats"]
        if not isinstance(seats_raw, list):
            raise _roster_invalid(f"复核员{reviewer_id}的席位须为列表")
        seats: list[tuple[date, int]] = []
        seen_dates: set[date] = set()
        for seat in seats_raw:
            if not isinstance(seat, dict) or set(seat) != {"date", "count"}:
                raise _roster_invalid("席位须含date与count")
            day_raw, count = seat["date"], seat["count"]
            if not isinstance(day_raw, str):
                raise _roster_invalid("席位日期须为ISO字符串")
            try:
                day = date.fromisoformat(day_raw.strip())
            except ValueError as exc:
                raise _roster_invalid(f"席位日期无法解析: {day_raw}") from exc
            if day in seen_dates:
                raise _roster_invalid(f"复核员{reviewer_id}在{day.isoformat()}含重复席位")
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise _roster_invalid("席位数量须为正整数")
            seen_dates.add(day)
            seats.append((day, count))
        reviewers[reviewer_id] = {
            "reviewer_id": reviewer_id,
            "skills": frozenset(skills),
            "budget_limit": _parse_budget_limit(item["budget_limit"]),
            "seats": tuple(sorted(seats)),
        }

    notice_specs: dict[str, dict[str, str]] = {}
    for item in notices_raw:
        if not isinstance(item, dict) or set(item) != {"notice_id", "required_skill", "designated_reviewer"}:
            raise _roster_invalid("公告复核要求须含notice_id、required_skill与designated_reviewer")
        notice_id = item["notice_id"]
        if not all(_nonempty_str(item[key]) for key in item):
            raise _roster_invalid("公告复核要求字段须为非空字符串")
        if notice_id in notice_specs:
            raise _roster_invalid(f"复核清单含重复公告要求: {notice_id}")
        if item["designated_reviewer"] not in reviewers:
            raise _roster_invalid(f"公告{notice_id}的原指定复核员未声明: {item['designated_reviewer']}")
        notice_specs[notice_id] = {
            "required_skill": item["required_skill"],
            "designated_reviewer": item["designated_reviewer"],
        }

    delegations: dict[str, dict[str, dict[str, str]]] = {}
    for item in delegations_raw:
        if not isinstance(item, dict) or set(item) != {"notice_id", "from", "to", "detail_sha256"}:
            raise _roster_invalid("转派授权须含notice_id、from、to与detail_sha256")
        notice_id, source, delegate = item["notice_id"], item["from"], item["to"]
        if not all(_nonempty_str(item[key]) for key in ("notice_id", "from", "to")):
            raise _roster_invalid("转派授权的notice_id、from、to须为非空字符串")
        if notice_id not in notice_specs:
            raise _roster_invalid(f"转派授权指向未声明复核要求的公告: {notice_id}")
        if source != notice_specs[notice_id]["designated_reviewer"]:
            raise _roster_invalid(f"转派授权来源须为公告{notice_id}的原指定复核员")
        if delegate not in reviewers:
            raise _roster_invalid(f"转派授权对象未声明为复核员: {delegate}")
        if delegate == source:
            raise _roster_invalid("转派对象不得与原指定复核员相同")
        if not _is_sha256(item["detail_sha256"]):
            raise _roster_invalid("转派授权须绑定64位小写十六进制详情SHA")
        if delegate in delegations.setdefault(notice_id, {}):
            raise _roster_invalid(f"公告{notice_id}含指向{delegate}的重复转派授权")
        delegations[notice_id][delegate] = {
            "notice_id": notice_id, "from": source, "to": delegate,
            "detail_sha256": item["detail_sha256"],
        }

    parsed = {"notices": notice_specs, "reviewers": reviewers, "delegations": delegations}
    return parsed, {"path": relative_posix(target, root), "sha256": sha256_bytes(raw)}


def _delegation_evidence(declared: dict[str, dict[str, str]], detail_sha: str | None) -> list[dict[str, Any]]:
    """逐条核对转派授权与当前详情SHA；只有SHA一致时授权才有效。"""
    evidence = []
    for target in sorted(declared):
        record = declared[target]
        if detail_sha is None:
            valid, reasons = False, ["detail_missing"]
        elif record["detail_sha256"] == detail_sha:
            valid, reasons = True, []
        else:
            valid, reasons = False, ["delegation_stale_sha"]
        evidence.append({
            "from": record["from"], "to": target,
            "detail_sha256": record["detail_sha256"],
            "valid": valid, "reasons": reasons,
        })
    return evidence


def _reviewer_edge(ctx: dict[str, Any], reviewer: dict[str, Any], contract: str) -> dict[str, Any]:
    """计算一名复核员对一条公告的可行边及全部阻断原因。"""
    reviewer_id = reviewer["reviewer_id"]
    spec = ctx["spec"]
    if reviewer_id == spec["designated_reviewer"]:
        authorization, auth_reasons = "designated", []
    elif contract == "designated_only":
        authorization, auth_reasons = "none", ["not_designated"]
    else:
        delegation = ctx["delegation_by_to"].get(reviewer_id)
        if delegation is None:
            authorization, auth_reasons = "none", ["delegation_absent"]
        elif not delegation["valid"]:
            authorization, auth_reasons = "none", ["delegation_stale_sha"]
        else:
            authorization, auth_reasons = "delegation", []
    skill_ok = spec["required_skill"] in reviewer["skills"]
    budget_ok = ctx["budget"] <= reviewer["budget_limit"]
    pre_deadline = [day for day, _count in reviewer["seats"] if day < ctx["deadline"]]
    earliest = min(pre_deadline).isoformat() if pre_deadline else None
    if pre_deadline:
        seat_reasons: list[str] = []
    elif reviewer["seats"]:
        seat_reasons = ["seat_expired"]
    else:
        seat_reasons = ["seat_unavailable"]
    reasons = sorted(
        auth_reasons
        + ([] if skill_ok else ["skill_missing"])
        + ([] if budget_ok else ["budget_over_authority"])
        + seat_reasons
    )
    return {
        "reviewer": reviewer_id,
        "authorization": authorization,
        "skill_ok": skill_ok,
        "budget_ok": budget_ok,
        "seats_declared": sum(count for _day, count in reviewer["seats"]),
        "earliest_seat": earliest,
        "feasible": not reasons,
        "reasons": reasons,
    }


def _build_review_context(notice: dict[str, Any], roster: dict[str, Any], contract: str) -> dict[str, Any]:
    """把 intake 证据与复核清单声明合并为一条公告的规划上下文。"""
    detail = notice["detail"]
    detail_sha = detail["sha256"] if detail else None
    spec = roster["notices"].get(notice["notice_id"])
    declared = roster["delegations"].get(notice["notice_id"], {}) if spec else {}
    delegations = _delegation_evidence(declared, detail_sha)
    ctx: dict[str, Any] = {
        "notice_id": notice["notice_id"],
        "intake": notice,
        "spec": spec,
        "delegations": delegations,
        "delegation_by_to": {item["to"]: item for item in delegations},
        "eligible": False,
        "budget": None,
        "deadline": None,
        "edges": [],
        "action": None,
        "reasons": [],
    }
    if notice["status"] != "coherent":
        # 不一致或缺详情的公告不进入席位分配，但保留全部来源与授权证据。
        ctx["reasons"] = ["missing_detail" if notice["status"] == "missing_detail" else "mismatch"]
        return ctx
    if spec is None:
        ctx["reasons"] = ["roster_entry_missing"]
        return ctx
    ctx["budget"] = Decimal(detail["budget"])
    ctx["deadline"] = date.fromisoformat(detail["deadline"])
    ctx["eligible"] = True
    ctx["edges"] = [
        _reviewer_edge(ctx, roster["reviewers"][reviewer_id], contract)
        for reviewer_id in sorted(roster["reviewers"])
    ]
    return ctx


def _assign_seats(eligible: list[dict[str, Any]], reviewers: dict[str, dict[str, Any]]) -> None:
    """联合规划：先最大化截止前完成数，再按截止日/公告ID优先，最后最小化转派。"""
    order = sorted(eligible, key=lambda ctx: (ctx["deadline"], ctx["notice_id"]))
    candidates: dict[str, list[tuple[str, str, str]]] = {}
    capacity: dict[tuple[str, str], int] = {}
    for ctx in order:
        options: list[tuple[str, str, str]] = []
        for edge in ctx["edges"]:
            if not edge["feasible"]:
                continue
            for day, _count in reviewers[edge["reviewer"]]["seats"]:
                if day < ctx["deadline"]:
                    options.append((edge["reviewer"], day.isoformat(), edge["authorization"]))
        options.sort(key=lambda item: (item[2] != "designated", item[0], item[1]))
        candidates[ctx["notice_id"]] = options
    for reviewer in reviewers.values():
        for day, count in reviewer["seats"]:
            capacity[(reviewer["reviewer_id"], day.isoformat())] = count

    best_key: tuple[Any, ...] | None = None
    best_assign: dict[str, tuple[str, str, str]] = {}

    def dfs(index: int, assign: dict[str, tuple[str, str, str]]) -> None:
        nonlocal best_key, best_assign
        if best_key is not None and len(assign) + (len(order) - index) < best_key[0]:
            return
        if index == len(order):
            flags = tuple(1 if ctx["notice_id"] in assign else 0 for ctx in order)
            transfers = sum(1 for item in assign.values() if item[2] == "delegation")
            key = (len(assign), flags, -transfers)
            if best_key is None or key > best_key:
                best_key = key
                best_assign = dict(assign)
            return
        ctx = order[index]
        notice_id = ctx["notice_id"]
        for reviewer_id, day_iso, via in candidates[notice_id]:
            slot = (reviewer_id, day_iso)
            if capacity.get(slot, 0) > 0:
                capacity[slot] -= 1
                assign[notice_id] = (reviewer_id, day_iso, via)
                dfs(index + 1, assign)
                del assign[notice_id]
                capacity[slot] += 1
        dfs(index + 1, assign)

    dfs(0, {})
    by_id = {ctx["notice_id"]: ctx for ctx in eligible}
    for notice_id, (reviewer_id, day_iso, via) in best_assign.items():
        by_id[notice_id]["action"] = {"reviewer": reviewer_id, "seat_date": day_iso, "via": via}


def _block_reasons(ctx: dict[str, Any]) -> list[str]:
    """为可行但未获席位的公告推导稳定阻断原因。"""
    edges = ctx["edges"]
    authorized = [edge for edge in edges if edge["authorization"] != "none"]
    skilled = [edge for edge in authorized if edge["skill_ok"]]
    budgeted = [edge for edge in skilled if edge["budget_ok"]]
    seated = [edge for edge in budgeted if edge["earliest_seat"] is not None]
    reasons = set()
    if any(not item["valid"] and "delegation_stale_sha" in item["reasons"] for item in ctx["delegations"]):
        reasons.add("delegation_stale_sha")
    if not skilled:
        reasons.add("skill_missing")
    elif not budgeted:
        reasons.add("budget_over_authority")
    elif not seated:
        if any(edge["seats_declared"] > 0 for edge in budgeted):
            reasons.add("seat_expired")
        else:
            reasons.add("seat_unavailable")
    else:
        reasons.add("seat_contended")
    return sorted(reasons)


def plan_review_actions(manifest_path: str | Path, roster_path: str | Path, *,
                        contract: str, root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """核对公告材料并规划截止前的复核席位行动；全程只读本地文件。

    contract="designated_only" 时公告只能交给原指定复核员；
    contract="delegation_allowed" 时另允许绑定当前详情SHA的有效转派。
    """
    if contract not in REVIEW_CONTRACTS:
        raise InputError("REVIEW_CONTRACT_UNKNOWN", f"未知复核合同: {contract}")
    root = root.resolve()
    intake = intake_notices(manifest_path, root=root)
    roster, roster_meta = _load_roster(roster_path, root)
    contexts = [_build_review_context(notice, roster, contract) for notice in intake["notices"]]
    eligible = [ctx for ctx in contexts if ctx["eligible"]]
    if len(eligible) > _MAX_PLAN_NOTICES:
        raise InputError("REVIEW_PLAN_TOO_LARGE", f"联合规划公告数超过上限: {len(eligible)}")
    _assign_seats(eligible, roster["reviewers"])
    for ctx in eligible:
        if ctx["action"] is None:
            ctx["reasons"] = _block_reasons(ctx)

    entries = []
    for ctx in contexts:
        notice = ctx["intake"]
        detail = notice["detail"]
        entries.append({
            "notice_id": ctx["notice_id"],
            "intake_status": notice["status"],
            "intake_reasons": notice["reasons"],
            "required_skill": ctx["spec"]["required_skill"] if ctx["spec"] else None,
            "designated_reviewer": ctx["spec"]["designated_reviewer"] if ctx["spec"] else None,
            "budget": detail["budget"] if detail else None,
            "deadline": detail["deadline"] if detail else None,
            "list": notice["list"],
            "detail": detail,
            "delegations": ctx["delegations"],
            "edges": ctx["edges"],
            "actionable": ctx["action"] is not None,
            "action": ctx["action"],
            "reasons": ctx["reasons"],
        })

    seat_usage = []
    for reviewer_id in sorted(roster["reviewers"]):
        reviewer = roster["reviewers"][reviewer_id]
        for day, count in reviewer["seats"]:
            occupied = sorted(
                ctx["notice_id"] for ctx in eligible
                if ctx["action"] and ctx["action"]["reviewer"] == reviewer_id
                and ctx["action"]["seat_date"] == day.isoformat()
            )
            seat_usage.append({
                "reviewer": reviewer_id, "date": day.isoformat(),
                "capacity": count, "used": len(occupied), "occupied_by": occupied,
            })

    actionable_count = sum(1 for entry in entries if entry["actionable"])
    return {
        "schema_version": 1,
        "contract": contract,
        "manifest": intake["manifest"],
        "roster": roster_meta,
        "status": "ready" if entries and all(entry["actionable"] for entry in entries) else "blocked",
        "summary": {
            "notice_count": len(entries),
            "coherent_count": sum(1 for entry in entries if entry["intake_status"] == "coherent"),
            "actionable_count": actionable_count,
            "blocked_count": len(entries) - actionable_count,
            "delegation_count": sum(
                1 for entry in entries if entry["action"] and entry["action"]["via"] == "delegation"
            ),
        },
        "notices": entries,
        "seat_usage": seat_usage,
    }
