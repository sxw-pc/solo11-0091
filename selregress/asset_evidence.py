"""离线清点本地授权照片：可复核的派生证据与按合同的许可单元裁决。

只处理 manifest 锁定的本地 PPM P6 素材，不联网、不写回配方、不删除图片。

两种事前许可合同各自独立裁决，互不替代：

- ``exact_copy``：仅 SHA-256 字节完全相同的资产合并为一个使用单元。
- ``derivative_family``：同一底图经过有界几何变换（90 度倍数旋转 +
  每边不超过上限的边框裁切）且重叠有效主体逐像素同色时合并为一个照片家族；
  仅共用模板背景但商品主体颜色/形状不同的图片必须拒合并。

实现刻意不使用全图灰度哈希或平均颜色单阈值——共享棋盘背景会主导这类度量，
而 A/B 的旋转裁切关系只能通过“对齐后只比较主体像素”发现。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .matcher import match_nodes
from .models import FieldSpec
from .util import PROJECT_ROOT, sha256_bytes, sha256_file

PPM_HEADER_RE = re.compile(rb"P6\n([0-9]+) ([0-9]+)\n(255)\n")
SUBJECT_CHROMA_THRESHOLD = 40
MIN_SUBJECT_PIXELS = 16
ANCHOR_LIMIT = 8

# derivative_family 的有界变换与证据阈值（manifest 可用 derivative_policy 覆盖）。
DEFAULT_POLICY: dict[str, object] = {
    "allowed_rotations_degrees": [0, 90, 180, 270],
    "max_crop_per_edge_px": 8,
    "min_subject_match_rate": 0.95,
    # 双向覆盖率必须接近全主体：否则把对齐窗口偏移到主体局部，就能把被篡改/异色
    # 像素移出窗口、用小块同色主体骗取 match_rate=1.0（会自动少计，必须杜绝）。
    "min_subject_coverage_each_side": 0.98,
    # 匹配主体像素的绝对下限：只重叠零星几个主体时比率没有统计意义。
    "min_subject_pixels_matched": 32,
    "subject_chroma_threshold": SUBJECT_CHROMA_THRESHOLD,
}

CONTRACT_EXACT_COPY = "exact_copy"
CONTRACT_DERIVATIVE_FAMILY = "derivative_family"
KNOWN_CONTRACTS = (CONTRACT_EXACT_COPY, CONTRACT_DERIVATIVE_FAMILY)


class AssetEvidenceError(ValueError):
    """输入不可信或证据不足；调用方不得在此情况下自动少计许可单元。"""


# --------------------------------------------------------------------------- #
# PPM 解析与主体掩码
# --------------------------------------------------------------------------- #
def parse_ppm_p6(raw: bytes, source_label: str) -> dict[str, object]:
    """严格解析本地 PPM P6 字节，保留可复核的原始像素来源。"""
    match = PPM_HEADER_RE.match(raw)
    if not match:
        raise AssetEvidenceError(f"资产必须是本地 PPM P6 文件: {source_label}")
    width, height = int(match[1]), int(match[2])
    if width <= 0 or height <= 0:
        raise AssetEvidenceError(f"PPM 尺寸非法: {source_label}")
    expected = match.end() + 3 * width * height
    if len(raw) != expected:
        raise AssetEvidenceError(f"PPM 像素字节与头声明不符，疑似损坏: {source_label}")
    payload = raw[match.end():]
    rgb = [
        (payload[3 * i], payload[3 * i + 1], payload[3 * i + 2])
        for i in range(width * height)
    ]
    return {
        "width": width,
        "height": height,
        "header": raw[: match.end()].decode("ascii"),
        "rgb": rgb,
        "pixel_byte_offset": match.end(),
        "raw_sha256": sha256_bytes(raw),
    }


def _is_subject(pixel: tuple[int, int, int], chroma_threshold: int) -> bool:
    """有效商品主体 = 彩色像素；灰阶棋盘背景与黑色底座都被排除。"""
    red, green, blue = pixel
    return max(red, green, blue) - min(red, green, blue) >= chroma_threshold


def build_subject_mask(
    rgb: list[tuple[int, int, int]], width: int, height: int, chroma_threshold: int
) -> list[bool]:
    return [_is_subject(pixel, chroma_threshold) for pixel in rgb]


def _subject_bbox(mask: list[bool], width: int, height: int) -> tuple[int, int, int, int]:
    xs: list[int] = []
    ys: list[int] = []
    for y in range(height):
        for x in range(width):
            if mask[y * width + x]:
                xs.append(x)
                ys.append(y)
    if not xs:
        return (0, 0, 0, 0)
    return (min(xs), min(ys), max(xs), max(ys))


def _anchor_pixels(
    rgb: list[tuple[int, int, int]],
    mask: list[bool],
    width: int,
    height: int,
    header_offset: int,
    limit: int = ANCHOR_LIMIT,
) -> list[dict[str, object]]:
    """确定性抽样主体锚点，保存原始像素坐标与 RGB，供人工复核。"""
    points = [
        (x, y)
        for y in range(height)
        for x in range(width)
        if mask[y * width + x]
    ]
    if len(points) <= limit:
        chosen = points
    else:
        step = len(points) / limit
        chosen = [points[int(k * step)] for k in range(limit)]
    return [
        {
            "x": x,
            "y": y,
            "rgb": list(rgb[y * width + x]),
            "raw_byte_offset": header_offset + 3 * (y * width + x),
        }
        for x, y in chosen
    ]


# --------------------------------------------------------------------------- #
# 90 度倍数旋转（同时旋转 RGB 与主体掩码，保证只比较主体、不比较模板背景）
# --------------------------------------------------------------------------- #
def rotate_rgb90(
    rgb: list[tuple[int, int, int]], width: int, height: int, degrees: int
) -> tuple[list[tuple[int, int, int]], int, int]:
    """顺时针旋转 90 度倍数，输出按新图行主序展平。

    - 90°CW：新尺寸 (H, W)，new[ox, H-1-oy] = old[oy, ox]
    - 180°：尺寸不变，new[W-1-ox, H-1-oy] = old[oy, ox]
    - 270°CW：新尺寸 (H, W)，new[W-1-ox, oy] = old[oy, ox]
    """
    if degrees % 90:
        raise AssetEvidenceError("仅允许 90 度倍数旋转")
    turns = (degrees // 90) % 4
    if turns == 0:
        return list(rgb), width, height
    if turns == 2:
        out = [
            rgb[(height - 1 - y) * width + (width - 1 - x)]
            for y in range(height)
            for x in range(width)
        ]
        return out, width, height

    # 旋转后新宽度=原 height，新高度=原 width；逆映射填充保证不重不漏
    new_width, new_height = height, width
    out: list[tuple[int, int, int]] = [None] * (new_width * new_height)  # type: ignore[list-item]
    for ny in range(new_height):  # 新行 0..width-1
        for nx in range(new_width):  # 新列 0..height-1
            if turns == 1:  # 90 CW: new[ox, H-1-oy]=old → old(oy=H-1-nx, ox=ny)
                oy, ox = height - 1 - nx, ny
            else:  # 270 CW: new[oy, W-1-ox]=old → old(oy=nx, ox=W-1-ny)
                oy, ox = nx, width - 1 - ny
            out[ny * new_width + nx] = rgb[oy * width + ox]
    return out, new_width, new_height

def rotate_mask90(
    mask: list[bool], width: int, height: int, degrees: int
) -> tuple[list[bool], int, int]:
    """与 rotate_rgb90 完全一致的坐标约定，直接旋转布尔掩码。"""
    if degrees % 90:
        raise AssetEvidenceError("仅允许 90 度倍数旋转")
    turns = (degrees // 90) % 4
    if turns == 0:
        return list(mask), width, height
    if turns == 2:
        out = [
            mask[(height - 1 - y) * width + (width - 1 - x)]
            for y in range(height)
            for x in range(width)
        ]
        return out, width, height

    new_width, new_height = height, width
    out = [False] * (new_width * new_height)
    for ny in range(new_height):
        for nx in range(new_width):
            if turns == 1:  # 90 CW 逆映射
                oy, ox = height - 1 - nx, ny
            else:  # 270 CW 逆映射
                oy, ox = nx, width - 1 - ny
            out[ny * new_width + nx] = mask[oy * width + ox]
    return out, new_width, new_height


# --------------------------------------------------------------------------- #
# 有界对齐：枚举 90 度倍数旋转与有限边框裁切，只统计重叠主体
# --------------------------------------------------------------------------- #
def _overlay_score(
    base_rgb: list[tuple[int, int, int]],
    base_mask: list[bool],
    base_w: int,
    base_h: int,
    cand_rgb: list[tuple[int, int, int]],
    cand_mask: list[bool],
    cand_w: int,
    cand_h: int,
    top: int,
    left: int,
) -> dict[str, object]:
    """候选图（已旋转）覆盖底图 [top:top+h, left:left+w] 区域时的证据。

    合并与否只看主体像素；``pixel_agreement`` 统计全重叠区（含模板背景），
    仅用于在多个主体达标的对齐里选出真实配准，不参与合并门槛。
    """
    base_subject_total = sum(base_mask)
    cand_subject_total = sum(cand_mask)
    base_hit = 0          # 底图主体落在对齐窗口内的数量
    base_intersect = 0    # 其中与候选主体重叠的数量
    cand_hit = 0          # 候选主体（全部位于自身图内，即对齐窗口内）数量
    matched = 0
    pixels_compared = 0
    pixels_equal = 0
    mismatched: list[dict[str, object]] = []
    for cy in range(cand_h):
        for cx in range(cand_w):
            c_idx = cy * cand_w + cx
            bx, by = left + cx, top + cy
            b_idx = by * base_w + bx
            pixels_compared += 1
            if base_rgb[b_idx] == cand_rgb[c_idx]:
                pixels_equal += 1
            in_base = base_mask[b_idx]
            in_cand = cand_mask[c_idx]
            if in_base:
                base_hit += 1
            if in_cand:
                cand_hit += 1
            if in_base and in_cand:
                base_intersect += 1
                if base_rgb[b_idx] == cand_rgb[c_idx]:
                    matched += 1
                elif len(mismatched) < ANCHOR_LIMIT:
                    mismatched.append(
                        {
                            "base_x": bx,
                            "base_y": by,
                            "base_rgb": list(base_rgb[b_idx]),
                            "candidate_rgb": list(cand_rgb[c_idx]),
                        }
                    )
    # 底图落在对齐窗口外的主体像素（被候选图裁掉或错位漏掉）必须计入未覆盖，
    # 否则偏移到主体局部的错误对齐会得到虚假的 1.0 覆盖率（防止自动少计）。
    base_subject_outside_window = base_subject_total - base_hit
    overlap_subjects = matched + len(mismatched)
    return {
        "base_subject_total": base_subject_total,
        "candidate_subject_total": cand_subject_total,
        "base_subject_covered": base_intersect,
        "base_subject_outside_window": base_subject_outside_window,
        "candidate_subject_overlapping": cand_hit,
        "subject_matched": matched,
        "subject_mismatched_sample": mismatched,
        # 双向覆盖率都相对各自主体总量；窗口外主体直接拉低 base_coverage。
        "base_coverage": base_intersect / base_subject_total if base_subject_total else 0.0,
        "candidate_coverage": cand_hit / cand_subject_total if cand_subject_total else 0.0,
        "match_rate": (matched / overlap_subjects) if overlap_subjects else 0.0,
        "pixels_compared": pixels_compared,
        "pixel_agreement": pixels_equal / pixels_compared if pixels_compared else 0.0,
        "center_penalty": abs(top - (base_h - cand_h) / 2)
        + abs((base_h - cand_h) - top - (base_h - cand_h) / 2)
        + abs(left - (base_w - cand_w) / 2)
        + abs((base_w - cand_w) - left - (base_w - cand_w) / 2),
    }


def _candidate_alignments(
    base: dict[str, object], candidate: dict[str, object], policy: dict[str, object]
) -> list[dict[str, object]]:
    max_crop = int(policy["max_crop_per_edge_px"])
    min_match = float(policy["min_subject_match_rate"])
    min_coverage = float(policy["min_subject_coverage_each_side"])
    min_matched = int(policy["min_subject_pixels_matched"])
    base_w, base_h = base["width"], base["height"]
    base_rgb, base_mask = base["rgb"], base["subject_mask"]
    attempts: list[dict[str, object]] = []
    for degrees in policy["allowed_rotations_degrees"]:
        rot_rgb, rot_w, rot_h = rotate_rgb90(
            candidate["rgb"], candidate["width"], candidate["height"], degrees
        )
        rot_mask, _, _ = rotate_mask90(
            candidate["subject_mask"], candidate["width"], candidate["height"], degrees
        )
        if rot_w > base_w or rot_h > base_h:
            continue
        # 旋转后候选图相对底图每边缺失的像素即裁切量，必须四边都在有界范围内。
        h_margin, v_margin = base_w - rot_w, base_h - rot_h
        for top in range(v_margin + 1):
            bottom = v_margin - top
            for left in range(h_margin + 1):
                right = h_margin - left
                crops = (top, right, bottom, left)
                if max(crops) > max_crop:
                    continue
                score = _overlay_score(
                    base_rgb, base_mask, base_w, base_h,
                    rot_rgb, rot_mask, rot_w, rot_h, top, left,
                )
                score.update(
                    {
                        "rotation_degrees_cw": degrees,
                        "crop_top_px": top,
                        "crop_right_px": right,
                        "crop_bottom_px": bottom,
                        "crop_left_px": left,
                        "max_crop_per_edge_px": max(crops),
                        "passes": (
                            score["match_rate"] >= min_match
                            and score["base_coverage"] >= min_coverage
                            and score["candidate_coverage"] >= min_coverage
                            and score["subject_matched"] >= min_matched
                        ),
                    }
                )
                attempts.append(score)
    return attempts


def _background_agreement(
    base: dict[str, object], candidate: dict[str, object], policy: dict[str, object]
) -> dict[str, object]:
    """量化“共享模板背景但主体不同”的反例：背景高度一致仍必须拒合并。

    取全重叠像素一致性最高的对齐，再只统计双方都是非主体（模板背景）的像素。
    """
    attempts = _candidate_alignments(base, candidate, policy)
    if not attempts:
        return {"template_pixels_compared": 0, "template_agreement": 0.0}
    chosen = max(attempts, key=lambda item: (item["pixel_agreement"], -item["center_penalty"]))
    base_w, base_h = base["width"], base["height"]
    base_rgb, base_mask = base["rgb"], base["subject_mask"]
    cand_w, cand_h = candidate["width"], candidate["height"]
    cand_rgb, cand_mask = candidate["rgb"], candidate["subject_mask"]
    top, left = chosen["crop_top_px"], chosen["crop_left_px"]
    compared = agreed = 0
    for cy in range(cand_h):
        for cx in range(cand_w):
            b_idx = (top + cy) * base_w + (left + cx)
            c_idx = cy * cand_w + cx
            if not base_mask[b_idx] and not cand_mask[c_idx]:
                compared += 1
                if base_rgb[b_idx] == cand_rgb[c_idx]:
                    agreed += 1
    return {
        "template_pixels_compared": compared,
        "template_agreement": round(agreed / compared, 6) if compared else 0.0,
        "registered_with_rotation_degrees_cw": chosen["rotation_degrees_cw"],
    }


def _subject_color_signature(asset: dict[str, object]) -> list[list[int]]:
    colors = sorted(
        {
            asset["rgb"][i]
            for i, flag in enumerate(asset["subject_mask"])
            if flag
        }
    )
    return [list(color) for color in colors]


def evaluate_derivative(
    base: dict[str, object], candidate: dict[str, object], policy: dict[str, object]
) -> dict[str, object]:
    """对单个有序资产对给出可复核的派生裁决与全部对齐参数。"""
    attempts = _candidate_alignments(base, candidate, policy)
    passing = [item for item in attempts if item["passes"]]

    def rank_key(item: dict[str, object]) -> tuple[float, float, float, float, int]:
        # 主体门槛达标后，用全重叠像素一致性定位真实配准；
        # 同分时偏向居中裁切与更小旋转，仍同分则视为歧义拒绝（不会自动少计）。
        return (
            round(item["pixel_agreement"], 6),
            round(item["match_rate"], 6),
            round(item["base_coverage"] + item["candidate_coverage"], 6),
            -float(item["center_penalty"]),
            -int(item["rotation_degrees_cw"]),
        )

    rejected_reason = ""
    best = None
    if not attempts:
        rejected_reason = "transform_outside_bounds"
    elif not passing:
        best = max(attempts, key=rank_key)
        if best["match_rate"] < float(policy["min_subject_match_rate"]):
            rejected_reason = "subject_color_or_shape_mismatch"
        else:
            rejected_reason = "subject_coverage_below_threshold"
    else:
        best = max(passing, key=rank_key)
        tied = [
            item for item in passing
            if rank_key(item) == rank_key(best)
            and (
                item["rotation_degrees_cw"] != best["rotation_degrees_cw"]
                or (item["crop_top_px"], item["crop_left_px"])
                != (best["crop_top_px"], best["crop_left_px"])
            )
        ]
        if tied:
            rejected_reason = "ambiguous_alignment"
            best = None

    background = _background_agreement(base, candidate, policy)
    evidence: dict[str, object] = {
        "base_asset": base["asset_id"],
        "candidate_asset": candidate["asset_id"],
        "base_subject_pixels": sum(base["subject_mask"]),
        "candidate_subject_pixels": sum(candidate["subject_mask"]),
        "base_subject_colors_rgb": _subject_color_signature(base),
        "candidate_subject_colors_rgb": _subject_color_signature(candidate),
        "background": background,
        "alignment_attempts": len(attempts),
        "bounded_policy": policy,
    }
    if best is not None:
        evidence["best_alignment"] = {
            key: best[key]
            for key in (
                "rotation_degrees_cw", "crop_top_px", "crop_right_px",
                "crop_bottom_px", "crop_left_px", "max_crop_per_edge_px",
                "base_coverage", "candidate_coverage", "match_rate",
                "pixel_agreement", "pixels_compared",
                "base_subject_covered", "base_subject_outside_window",
                "candidate_subject_overlapping",
                "subject_matched", "subject_mismatched_sample",
            )
        }
    if rejected_reason:
        evidence["reject_reason"] = rejected_reason
        evidence["accepted"] = False
    else:
        evidence["accepted"] = True
        evidence["aligned_pixel_source"] = _aligned_anchor_source(base, candidate, best)
    return evidence


def _aligned_anchor_source(
    base: dict[str, object], candidate: dict[str, object], alignment: dict[str, object]
) -> dict[str, object]:
    """记录候选原始像素 → 底图对齐坐标的映射公式与锚点，保留双方像素来源。"""
    degrees = alignment["rotation_degrees_cw"]
    top, left = alignment["crop_top_px"], alignment["crop_left_px"]
    anchors = []
    for anchor in candidate["pixel_source"]["anchors"][: ANCHOR_LIMIT // 2]:
        cx, cy = anchor["x"], anchor["y"]
        # 与 rotate_rgb90 相同的坐标约定（把候选像素映回旋转后坐标系）：
        # 90 CW: 新(列=H-1-cy, 行=cx)；270 CW: 新(列=cy, 行=W-1-cx)
        if degrees == 0:
            rx, ry = cx, cy
        elif degrees == 90:
            rx, ry = candidate["height"] - 1 - cy, cx
        elif degrees == 180:
            rx, ry = candidate["width"] - 1 - cx, candidate["height"] - 1 - cy
        else:  # 270 CW
            rx, ry = cy, candidate["width"] - 1 - cx
        bx, by = left + rx, top + ry
        b_idx = by * base["width"] + bx
        anchors.append(
            {
                "candidate_x": cx,
                "candidate_y": cy,
                "candidate_rgb": anchor["rgb"],
                "aligned_base_x": bx,
                "aligned_base_y": by,
                "base_rgb": list(base["rgb"][b_idx]),
            }
        )
    return {
        "mapping": (
            "rotate candidate clockwise by rotation_degrees_cw, then place its "
            "origin at base (crop_top_px, crop_left_px); base_x = left + rotated_x, "
            "base_y = top + rotated_y"
        ),
        "rotation_degrees_cw": degrees,
        "base_offset_top": top,
        "base_offset_left": left,
        "anchors": anchors,
    }


# --------------------------------------------------------------------------- #
# 家族归并与按合同裁决
# --------------------------------------------------------------------------- #
def _family_groups_from_members(
    assets: list[dict[str, object]], buckets: dict[str, list[str]]
) -> list[dict[str, object]]:
    groups = []
    for index, key in enumerate(sorted(buckets), start=1):
        member_ids = sorted(buckets[key])
        members = [next(a for a in assets if a["asset_id"] == m) for m in member_ids]
        groups.append(
            {
                "family_id": f"family-{index}",
                "asset_ids": member_ids,
                "skus": [m["sku"] for m in members],
                "sha256": sorted({m["sha256"] for m in members}),
                "unit_count": 1,
            }
        )
    return groups


def _edge_lookup(derivative_edges: list[dict[str, object]]) -> dict[tuple[str, str], dict[str, object]]:
    lookup: dict[tuple[str, str], dict[str, object]] = {}
    for edge in derivative_edges:
        lookup[(edge["base_asset"], edge["candidate_asset"])] = edge
        lookup[(edge["candidate_asset"], edge["base_asset"])] = edge
    return lookup


def assign_derivative_families(
    assets: list[dict[str, object]], derivative_edges: list[dict[str, object]]
) -> tuple[list[dict[str, object]], dict[str, str]]:
    """以最大底图为家族根做有界归并。

    按面积降序处理：每个资产只能并入一个“对它而言有界变换成立”的现存家族，
    或自立门户。这样深裁图（相对原始底图超出 max_crop）不会借助中间图 B
    被链式并入 A 的家族，杜绝自动少计。返回家族与 asset_id -> family_id。
    """
    edge_by_pair = _edge_lookup(derivative_edges)
    ordered = sorted(
        assets, key=lambda a: (-(a["width"] * a["height"]), a["asset_id"])
    )
    roots: list[str] = []
    members_of: dict[str, list[str]] = {}
    family_of: dict[str, str] = {}
    for asset in ordered:
        asset_id = asset["asset_id"]
        placed = ""
        for root_id in roots:
            edge = edge_by_pair.get((root_id, asset_id))
            if edge is not None and edge["accepted"]:
                placed = root_id
                break
        if not placed:
            roots.append(asset_id)
            members_of[asset_id] = [asset_id]
            family_of[asset_id] = asset_id
        else:
            members_of[placed].append(asset_id)
            family_of[asset_id] = placed
    buckets = {root: members_of[root] for root in roots}
    groups = _family_groups_from_members(assets, buckets)
    root_to_family = {root: group["family_id"] for root, group in zip(roots, groups)}
    return groups, {asset_id: root_to_family[family_of[asset_id]] for asset_id in family_of}


def decide_licenses(
    assets: list[dict[str, object]],
    derivative_edges: list[dict[str, object]],
    fee_per_unit: int,
) -> dict[str, object]:
    asset_ids = [asset["asset_id"] for asset in assets]

    # 合同一：exact_copy —— 只按字节 SHA 合并。
    sha_groups: dict[str, list[str]] = {}
    for asset in assets:
        sha_groups.setdefault(asset["sha256"], []).append(asset["asset_id"])
    exact_buckets: dict[str, list[str]] = {}
    for members in sha_groups.values():
        exact_buckets[members[0]] = list(members)
    exact_families = _family_groups_from_members(assets, exact_buckets)
    exact_units = len(exact_families)
    exact_family_of = {
        asset_id: family["family_id"]
        for family in exact_families
        for asset_id in family["asset_ids"]
    }

    # 合同二：derivative_family —— 有界几何变换 + 重叠主体逐像素同色，
    # 以最大底图为根归并，禁止跨有界边界的链式合并。
    derivative_families, family_of = assign_derivative_families(assets, derivative_edges)
    derivative_units = len(derivative_families)
    for edge in derivative_edges:
        if not edge["accepted"]:
            continue
        if edge["edge_kind"] == "bounded_geometric_transform":
            edge["family_id"] = family_of[edge["candidate_asset"]]
        else:  # exact_copy：标注其字节家族，便于交叉核对两份合同
            edge["family_id"] = exact_family_of[edge["candidate_asset"]]

    return {
        CONTRACT_EXACT_COPY: {
            "rule": "one license unit per distinct SHA-256 byte identity",
            "distinct_sha256": len(sha_groups),
            "photo_families": exact_families,
            "license_units": exact_units,
            "fee_per_unit": fee_per_unit,
            "total_fee": exact_units * fee_per_unit,
        },
        CONTRACT_DERIVATIVE_FAMILY: {
            "rule": (
                "one license unit per bounded photo family (90-degree multiple "
                "rotations and limited edge crops with matching subject pixels); "
                "shared template background with a different subject never merges"
            ),
            "photo_families": derivative_families,
            "license_units": derivative_units,
            "fee_per_unit": fee_per_unit,
            "total_fee": derivative_units * fee_per_unit,
        },
    }


# --------------------------------------------------------------------------- #
# 入口：从 manifest 锁定的 snapshot 因果性重算
# --------------------------------------------------------------------------- #
def _safe_local_path(base_dir: Path, raw_name: str, label: str) -> Path:
    if not raw_name or raw_name.strip() != raw_name:
        raise AssetEvidenceError(f"{label} 路径不可信: {raw_name!r}")
    candidate = Path(raw_name)
    if candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise AssetEvidenceError(f"{label} 禁止绝对路径或目录越界: {raw_name}")
    target = (base_dir / candidate).resolve()
    try:
        target.relative_to(base_dir)
    except ValueError as exc:
        raise AssetEvidenceError(f"{label} 解析后越界: {raw_name}") from exc
    if not target.is_file():
        raise AssetEvidenceError(f"{label} 指向的本地文件不存在: {raw_name}")
    return target


def inspect_asset_contract(manifest_path: str | Path) -> dict[str, object]:
    """读取 manifest 与本地 snapshot，逐字节核验并按两份合同裁决许可单元。

    每次调用都重新读取 img src 指向的实际字节：src 变化 → SHA/像素/家族/费用
    全部因果性重算，函数内不缓存任何旧结果。
    """
    manifest_file = Path(manifest_path).resolve()
    try:
        manifest_file.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise AssetEvidenceError(f"manifest 超出项目根目录: {manifest_path}") from exc
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("contracts") != list(KNOWN_CONTRACTS):
        raise AssetEvidenceError("unknown asset contracts")
    fee_per_unit = manifest["license_fee_per_unit"]
    if not isinstance(fee_per_unit, int) or fee_per_unit <= 0:
        raise AssetEvidenceError("license_fee_per_unit 必须是正整数")

    policy = dict(DEFAULT_POLICY)
    override = manifest.get("derivative_policy")
    if override is not None:
        if not isinstance(override, dict):
            raise AssetEvidenceError("derivative_policy 必须是对象")
        for key in DEFAULT_POLICY:
            if key in override:
                policy[key] = override[key]
    if sorted(policy["allowed_rotations_degrees"]) != [0, 90, 180, 270]:
        raise AssetEvidenceError("有界旋转策略只允许 0/90/180/270 度")

    snapshot = (PROJECT_ROOT / manifest["snapshot"]).resolve()
    try:
        snapshot.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise AssetEvidenceError(f"snapshot 超出项目根目录: {manifest['snapshot']}") from exc
    if not snapshot.is_file():
        raise AssetEvidenceError(f"snapshot 不可信或不存在: {manifest['snapshot']}")
    snapshot_dir = snapshot.parent
    soup = BeautifulSoup(snapshot.read_text(encoding="utf-8"), "html.parser")

    assets: list[dict[str, object]] = []
    for card in soup.select("article.asset"):
        sku = match_nodes(card, FieldSpec(name="sku", selectors=(".sku",)))
        image = match_nodes(
            card, FieldSpec(name="photo", selectors=("img.photo",), extract="src")
        )
        if sku.count != 1 or image.count != 1:
            raise AssetEvidenceError("asset card must have one sku and one image")
        sku_text = sku.nodes[0].get_text(" ", strip=True)
        if not sku_text:
            raise AssetEvidenceError("asset card sku 为空")
        src = image.nodes[0].get("src")
        if not isinstance(src, str):
            raise AssetEvidenceError("image src missing")
        image_file = _safe_local_path(snapshot_dir, src, "image src")
        raw = image_file.read_bytes()
        parsed = parse_ppm_p6(raw, src)
        width, height = parsed["width"], parsed["height"]
        mask = build_subject_mask(
            parsed["rgb"], width, height, int(policy["subject_chroma_threshold"])
        )
        subject_count = sum(mask)
        if subject_count < MIN_SUBJECT_PIXELS:
            raise AssetEvidenceError(f"有效主体像素过少，证据不足: {src}")
        bbox = _subject_bbox(mask, width, height)
        asset_id = f"asset-{len(assets) + 1}"
        asset = {
            "asset_id": asset_id,
            "sku": sku_text,
            "image_src": src,
            "image_path": image_file.relative_to(PROJECT_ROOT).as_posix(),
            "width": width,
            "height": height,
            "sha256": parsed["raw_sha256"],
            "subject_pixel_count": subject_count,
            "subject_bbox": {
                "x_min": bbox[0], "y_min": bbox[1], "x_max": bbox[2], "y_max": bbox[3],
            },
            "subject_coverage_ratio": round(subject_count / (width * height), 6),
            "subject_colors_rgb": None,
            "subject_mask": mask,
            "rgb": parsed["rgb"],
            "ppm_header": parsed["header"],
            "pixel_byte_offset": parsed["pixel_byte_offset"],
            "pixel_source": {
                "image_path": image_file.relative_to(PROJECT_ROOT).as_posix(),
                "sha256": parsed["raw_sha256"],
                "ppm_header": parsed["header"],
                "pixel_byte_offset": parsed["pixel_byte_offset"],
                "width": width,
                "height": height,
                "anchors": _anchor_pixels(
                    parsed["rgb"], mask, width, height, parsed["pixel_byte_offset"]
                ),
            },
        }
        asset["subject_colors_rgb"] = _subject_color_signature(asset)
        assets.append(asset)

    expected = manifest["expected_assets"]
    if len(assets) != expected or len({a["sku"] for a in assets}) != len(assets):
        raise AssetEvidenceError("asset count or sku identity mismatch")

    # 两两评估派生关系；相同 SHA 直接记录 exact_copy 边，不参与几何搜索。
    # 非相同 SHA 的资产对同时尝试两个方向（小图无法容纳大图的对齐会自然失败），
    # 任一方向在有界变换内通过即接受，但家族归并仍以最大底图为根（见下）。
    derivative_edges: list[dict[str, object]] = []
    rejected_merges: list[dict[str, object]] = []
    for i in range(len(assets)):
        for j in range(i + 1, len(assets)):
            first, second = assets[i], assets[j]
            if first["sha256"] == second["sha256"]:
                edge = {
                    "base_asset": first["asset_id"],
                    "candidate_asset": second["asset_id"],
                    "accepted": True,
                    "edge_kind": "exact_copy",
                    "base_subject_pixels": sum(first["subject_mask"]),
                    "candidate_subject_pixels": sum(second["subject_mask"]),
                    "best_alignment": {
                        "rotation_degrees_cw": 0,
                        "crop_top_px": 0, "crop_right_px": 0,
                        "crop_bottom_px": 0, "crop_left_px": 0,
                        "max_crop_per_edge_px": 0,
                        "base_coverage": 1.0, "candidate_coverage": 1.0,
                        "match_rate": 1.0,
                        "subject_matched": sum(first["subject_mask"]),
                        "subject_mismatched_sample": [],
                    },
                    "background": {"template_pixels_compared": 0, "template_agreement": 1.0},
                    "bounded_policy": policy,
                }
            else:
                forward = evaluate_derivative(first, second, policy)
                reverse = evaluate_derivative(second, first, policy)
                if forward["accepted"]:
                    edge = forward
                elif reverse["accepted"]:
                    # 仅当反向（second 为底图）在有界变换内成立时才反向归并
                    edge = reverse
                else:
                    # 两个方向都失败：保留证据更强（有实际对齐尝试）的一侧
                    edge = forward if forward.get("best_alignment") else reverse
                    if not edge.get("best_alignment") and reverse.get("best_alignment"):
                        edge = reverse
                edge["edge_kind"] = "bounded_geometric_transform"
                edge["reverse_direction_reject_reason"] = (
                    None if reverse["accepted"] else reverse.get("reject_reason")
                )
            derivative_edges.append(edge)
            if not edge["accepted"]:
                rejected_merges.append(
                    {
                        "base_asset": edge["base_asset"],
                        "candidate_asset": edge["candidate_asset"],
                        "reject_reason": edge["reject_reason"],
                        "background": edge["background"],
                        "best_alignment": edge.get("best_alignment"),
                        "base_subject_colors_rgb": edge["base_subject_colors_rgb"],
                        "candidate_subject_colors_rgb": edge["candidate_subject_colors_rgb"],
                    }
                )

    decisions = decide_licenses(assets, derivative_edges, fee_per_unit)

    public_assets = [
        {key: asset[key] for key in (
            "asset_id", "sku", "image_src", "image_path", "width", "height",
            "sha256", "subject_pixel_count", "subject_bbox", "subject_coverage_ratio",
            "subject_colors_rgb", "ppm_header", "pixel_byte_offset", "pixel_source",
        )}
        for asset in assets
    ]
    return {
        "snapshot": manifest["snapshot"],
        "snapshot_sha256": sha256_file(snapshot),
        "contracts": manifest["contracts"],
        "license_fee_per_unit": fee_per_unit,
        "bounded_transform_policy": policy,
        "assets": public_assets,
        "derivative_edges": derivative_edges,
        "rejected_merges": rejected_merges,
        "license_decisions": decisions,
    }
