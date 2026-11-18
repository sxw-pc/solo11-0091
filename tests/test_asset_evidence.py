import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from selregress.asset_evidence import (
    DEFAULT_POLICY,
    AssetEvidenceError,
    inspect_asset_contract,
    rotate_mask90,
    rotate_rgb90,
)
from selregress.util import PROJECT_ROOT

FIXTURE_DIR = PROJECT_ROOT / "fixtures/asset_contract"
MANIFEST = FIXTURE_DIR / "manifest.json"
EXTENDED_MANIFEST = FIXTURE_DIR / "manifest-extended.json"
SANDBOX = PROJECT_ROOT / "tests" / "_tmp" / "asset_evidence"

TWO_CARD_HTML = (
    "<html><body>"
    '<article class="asset"><span class="sku">s1</span><img class="photo" src="asset-a.ppm"/></article>'
    '<article class="asset"><span class="sku">s2</span><img class="photo" src="{src}"/></article>'
    "</body></html>"
)


@pytest.fixture()
def case_dir(request):
    target = SANDBOX / request.node.name
    shutil.rmtree(target, ignore_errors=True)
    target.mkdir(parents=True, exist_ok=True)
    for name in ("asset-a.ppm", "asset-b.ppm", "asset-c.ppm"):
        shutil.copy(FIXTURE_DIR / name, target / name)
    yield target
    shutil.rmtree(target, ignore_errors=True)


def _write_manifest(case_dir: Path, expected: int = 2, extra: dict | None = None) -> Path:
    rel = case_dir.relative_to(PROJECT_ROOT).as_posix()
    manifest = {
        "snapshot": f"{rel}/snapshot.html",
        "contracts": ["exact_copy", "derivative_family"],
        "license_fee_per_unit": 10,
        "expected_assets": expected,
    }
    if extra:
        manifest.update(extra)
    path = case_dir / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def _edge(result, base, candidate):
    for edge in result["derivative_edges"]:
        if edge["base_asset"] == base and edge["candidate_asset"] == candidate:
            return edge
    raise KeyError((base, candidate))


def _rejection(result, base, candidate):
    for item in result["rejected_merges"]:
        if item["base_asset"] == base and item["candidate_asset"] == candidate:
            return item
    raise KeyError((base, candidate))


# --------------------------------------------------------------------------- #
# 旋转/掩码坐标约定：用非正方形数值网格与夹具双向恒等式锁死
# --------------------------------------------------------------------------- #
def test_rotation_math_on_non_square_grid_and_fixture():
    width, height = 2, 3
    grid = [(y * 10 + x, 0, 0) for y in range(height) for x in range(width)]

    r90, new_w, new_h = rotate_rgb90(grid, width, height, 90)
    assert (new_w, new_h) == (height, width)  # 新宽=原高=3，新高=原宽=2
    expected_90 = [None] * 6
    for ny in range(new_h):  # 新行 0..W-1
        for nx in range(new_w):  # 新列 0..H-1
            expected_90[ny * new_w + nx] = grid[(height - 1 - nx) * width + ny]
    assert r90 == expected_90

    r270, r270_w, r270_h = rotate_rgb90(grid, width, height, 270)
    expected_270 = [None] * 6
    for ny in range(r270_h):  # 新行 0..W-1
        for nx in range(r270_w):  # 新列 0..H-1
            expected_270[ny * r270_w + nx] = grid[nx * width + (width - 1 - ny)]
    assert r270 == expected_270

    assert rotate_rgb90(grid, width, height, 180)[0] == list(reversed(grid))
    # 互逆：90 后 270 回到原图（中间图宽高已交换为 new_w × new_h）
    assert rotate_rgb90(r90, new_w, new_h, 270)[0] == grid
    assert rotate_rgb90(r270, r270_w, r270_h, 90)[0] == grid

    mask = [(value[0] % 2 == 0) for value in grid]
    for degrees in (90, 270):
        rotated_mask, _, _ = rotate_mask90(mask, width, height, degrees)
        rotated_rgb, rw, rh = rotate_rgb90(
            [(1 if flag else 0, 0, 0) for flag in mask], width, height, degrees
        )
        assert rotated_mask == [pixel[0] == 1 for pixel in rotated_rgb]

    # 夹具恒等式：B = rotate90cw(A) 后四边裁 6；其逆为 rotate270(B) 落回 A 窗口
    def load_ppm(name):
        raw = (FIXTURE_DIR / name).read_bytes()
        import re
        match = re.match(rb"P6\n([0-9]+) ([0-9]+)\n255\n", raw)
        w, h = int(match[1]), int(match[2])
        pixels = [
            tuple(raw[match.end() + 3 * (y * w + x) + c] for c in range(3))
            for y in range(h) for x in range(w)
        ]
        return w, h, pixels

    aw, ah, a_pixels = load_ppm("asset-a.ppm")
    bw, bh, b_pixels = load_ppm("asset-b.ppm")
    rotated_a = rotate_rgb90(a_pixels, aw, ah, 90)[0]
    cropped = [rotated_a[(6 + yy) * 64 + 6 + xx] for yy in range(52) for xx in range(52)]
    assert cropped == b_pixels
    rotated_b = rotate_rgb90(b_pixels, bw, bh, 270)[0]
    assert rotated_b == [a_pixels[(6 + yy) * 64 + 6 + xx] for yy in range(52) for xx in range(52)]


# --------------------------------------------------------------------------- #
# 锁定三图：两份合同各自裁决
# --------------------------------------------------------------------------- #
def test_locked_three_assets_byte_evidence():
    result = inspect_asset_contract(MANIFEST)
    assets = result["assets"]
    assert [x["sku"] for x in assets] == ["sku-red-1", "sku-red-2", "sku-blue-1"]
    assert [(x["width"], x["height"]) for x in assets] == [(64, 64), (52, 52), (64, 64)]
    assert len({x["sha256"] for x in assets}) == 3
    # 主体掩码只命中彩色商品，不被棋盘背景或黑色底座污染
    assert {x["subject_pixel_count"] for x in assets} == {590}
    red, blue = [210, 35, 45], [35, 55, 210]
    assert assets[0]["subject_colors_rgb"] == [red]
    assert assets[2]["subject_colors_rgb"] == [blue]
    # 原始像素来源可复核
    for asset in assets:
        source = asset["pixel_source"]
        assert source["sha256"] == asset["sha256"]
        assert source["ppm_header"].startswith("P6\n")
        assert source["pixel_byte_offset"] > 0
        assert source["anchors"] and all(
            len(a["rgb"]) == 3 and a["raw_byte_offset"] >= source["pixel_byte_offset"]
            for a in source["anchors"]
        )


def test_exact_copy_contract_counts_three_units():
    result = inspect_asset_contract(MANIFEST)
    decision = result["license_decisions"]["exact_copy"]
    assert decision["distinct_sha256"] == 3
    assert decision["license_units"] == 3
    assert decision["fee_per_unit"] == 10
    assert decision["total_fee"] == 30
    assert all(len(f["asset_ids"]) == 1 for f in decision["photo_families"])


def test_derivative_family_contract_merges_only_a_and_b():
    result = inspect_asset_contract(MANIFEST)
    decision = result["license_decisions"]["derivative_family"]
    assert decision["license_units"] == 2
    assert decision["total_fee"] == 20
    families = {tuple(f["asset_ids"]) for f in decision["photo_families"]}
    assert families == {("asset-1", "asset-2"), ("asset-3",)}

    edge = _edge(result, "asset-1", "asset-2")
    assert edge["accepted"] is True
    alignment = edge["best_alignment"]
    # B 相对 A 的有界变换：90 度倍数旋转 + 四边各裁 6px
    assert alignment["rotation_degrees_cw"] in {0, 90, 180, 270}
    assert (
        alignment["crop_top_px"], alignment["crop_right_px"],
        alignment["crop_bottom_px"], alignment["crop_left_px"],
    ) == (6, 6, 6, 6)
    assert alignment["max_crop_per_edge_px"] <= DEFAULT_POLICY["max_crop_per_edge_px"]
    assert alignment["base_coverage"] == 1.0
    assert alignment["candidate_coverage"] == 1.0
    assert alignment["match_rate"] == 1.0
    # 对齐后锚点 RGB 与底图逐点一致，映射公式与参数齐备
    source = edge["aligned_pixel_source"]
    assert source["rotation_degrees_cw"] == alignment["rotation_degrees_cw"]
    assert all(a["candidate_rgb"] == a["base_rgb"] for a in source["anchors"])


def test_blue_subject_rejected_despite_identical_template_background():
    result = inspect_asset_contract(MANIFEST)
    rejection = _rejection(result, "asset-1", "asset-3")
    assert rejection["reject_reason"] == "subject_color_or_shape_mismatch"
    # 反例关键：模板背景几乎完全相同，证明决策没有被共享背景误导
    assert rejection["background"]["template_agreement"] >= 0.99
    assert rejection["best_alignment"]["match_rate"] == 0.0
    # C 独立成一个家族，绝不能与 A/B 合并
    decision = result["license_decisions"]["derivative_family"]
    blue_family = next(
        f for f in decision["photo_families"] if "asset-3" in f["asset_ids"]
    )
    assert blue_family["asset_ids"] == ["asset-3"]


def test_two_contracts_never_substitute_each_other():
    result = inspect_asset_contract(MANIFEST)
    exact = result["license_decisions"]["exact_copy"]
    family = result["license_decisions"]["derivative_family"]
    # exact_copy 的 3 单元不得替代 derivative_family 的 2 单元
    assert (exact["license_units"], exact["total_fee"]) == (3, 30)
    assert (family["license_units"], family["total_fee"]) == (2, 20)


def test_output_is_deterministic_for_same_inputs():
    first = inspect_asset_contract(MANIFEST)
    second = inspect_asset_contract(MANIFEST)
    assert json.dumps(first, sort_keys=True, default=str) == json.dumps(
        second, sort_keys=True, default=str
    )


# --------------------------------------------------------------------------- #
# 扩展反例：exact copy、不同商品共模板、超界深裁
# --------------------------------------------------------------------------- #
def test_extended_fixture_exact_copies_merge_only_by_sha():
    result = inspect_asset_contract(EXTENDED_MANIFEST)
    decision = result["license_decisions"]["exact_copy"]
    # 6 张图、4 个不同 SHA（两组字节相同副本 + B + 深裁图）
    assert decision["distinct_sha256"] == 4
    assert decision["license_units"] == 4
    assert decision["total_fee"] == 40
    families = {tuple(f["asset_ids"]) for f in decision["photo_families"]}
    assert ("asset-1", "asset-2") in families
    assert ("asset-4", "asset-5") in families
    copy_edge = _edge(result, "asset-1", "asset-2")
    assert copy_edge["edge_kind"] == "exact_copy"
    assert copy_edge["accepted"] is True


def test_extended_fixture_deep_crop_stays_its_own_unit():
    result = inspect_asset_contract(EXTENDED_MANIFEST)
    decision = result["license_decisions"]["derivative_family"]
    # A 家族（A + 副本 + B）、C 家族（C + 副本）、超界深裁图各自独立
    assert decision["license_units"] == 3
    assert decision["total_fee"] == 30
    deep_family = next(
        f for f in decision["photo_families"] if "asset-6" in f["asset_ids"]
    )
    assert deep_family["asset_ids"] == ["asset-6"]
    # 深裁图相对原始底图每边缺失 10px，超过 8px 上限，即使主体 100% 保留也拒合并
    rejection = _rejection(result, "asset-6", "asset-1")
    assert rejection["reject_reason"] == "transform_outside_bounds"
    # 它与中间图 B 同样超界，不能被链式并入 A 家族
    assert _rejection(result, "asset-6", "asset-2")["reject_reason"] in {
        "transform_outside_bounds", "subject_coverage_below_threshold",
    }


def test_extended_fixture_blue_copies_never_join_red_family():
    result = inspect_asset_contract(EXTENDED_MANIFEST)
    decision = result["license_decisions"]["derivative_family"]
    red_family = next(
        f for f in decision["photo_families"] if "asset-1" in f["asset_ids"]
    )
    blue_family = next(
        f for f in decision["photo_families"] if "asset-4" in f["asset_ids"]
    )
    assert set(red_family["asset_ids"]) == {"asset-1", "asset-2", "asset-3"}
    assert set(blue_family["asset_ids"]) == {"asset-4", "asset-5"}
    for base in ("asset-1", "asset-2"):
        rejection = _rejection(result, base, "asset-4")
        assert rejection["reject_reason"] == "subject_color_or_shape_mismatch"
        assert rejection["background"]["template_agreement"] >= 0.99


# --------------------------------------------------------------------------- #
# 因果性：img src 或实际字节变化必须重算，不得沿用旧结果
# --------------------------------------------------------------------------- #
def test_src_swap_causally_recomputes_family_and_fee(case_dir):
    snapshot = case_dir / "snapshot.html"
    snapshot.write_text(TWO_CARD_HTML.format(src="asset-b.ppm"), encoding="utf-8")
    manifest_path = _write_manifest(case_dir)

    first = inspect_asset_contract(manifest_path)
    assert first["license_decisions"]["derivative_family"]["license_units"] == 1
    assert first["license_decisions"]["exact_copy"]["license_units"] == 2

    # src 改指 C：实际字节与 SHA 改变，家族与费用必须因果性重算为 2 单元
    snapshot.write_text(TWO_CARD_HTML.format(src="asset-c.ppm"), encoding="utf-8")
    second = inspect_asset_contract(manifest_path)
    swapped = second["assets"][1]
    assert swapped["image_src"] == "asset-c.ppm"
    assert swapped["sha256"] != first["assets"][1]["sha256"]
    assert second["license_decisions"]["derivative_family"]["license_units"] == 2
    assert second["license_decisions"]["derivative_family"]["total_fee"] == 20
    assert second["license_decisions"]["exact_copy"]["license_units"] == 2


def test_byte_mutation_changes_sha_and_decision_without_cache(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="asset-b.ppm"), encoding="utf-8"
    )
    # 该合同要求主体逐像素完全一致（零容差），任何主体像素损坏都必须拒合并
    manifest_path = _write_manifest(
        case_dir, extra={"derivative_policy": {"min_subject_match_rate": 1.0}}
    )
    before = inspect_asset_contract(manifest_path)
    assert before["license_decisions"]["derivative_family"]["license_units"] == 1
    original_sha = before["assets"][1]["sha256"]

    target = case_dir / "asset-b.ppm"
    raw = bytearray(target.read_bytes())
    # 直接改一个主体锚点像素的红色通道，破坏主体逐像素同色证据
    anchor = before["assets"][1]["pixel_source"]["anchors"][0]
    red_offset = anchor["raw_byte_offset"]
    assert raw[red_offset] == anchor["rgb"][0]
    raw[red_offset] = (raw[red_offset] + 40) % 256
    target.write_bytes(bytes(raw))

    after = inspect_asset_contract(manifest_path)
    assert after["assets"][1]["sha256"] != original_sha
    # 主体被改后不再是 A 的合规派生：不得继续按 1 个家族少计
    edge = _edge(after, "asset-1", "asset-2")
    assert edge["accepted"] is False
    assert edge["reject_reason"] == "subject_color_or_shape_mismatch"
    assert after["license_decisions"]["derivative_family"]["license_units"] == 2


# --------------------------------------------------------------------------- #
# 不可信输入：损坏、越界、缺料一律报错，绝不自动少计
# --------------------------------------------------------------------------- #
def test_corrupt_ppm_is_rejected(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="asset-b.ppm"), encoding="utf-8"
    )
    manifest_path = _write_manifest(case_dir)
    target = case_dir / "asset-b.ppm"
    raw = target.read_bytes()
    target.write_bytes(raw[: len(raw) - 7])  # 截断像素区
    with pytest.raises(AssetEvidenceError, match="像素字节|PPM"):
        inspect_asset_contract(manifest_path)


def test_path_traversal_src_is_rejected(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="../../selregress/util.py"), encoding="utf-8"
    )
    manifest_path = _write_manifest(case_dir)
    with pytest.raises(AssetEvidenceError, match="越界|禁止"):
        inspect_asset_contract(manifest_path)


def test_absolute_src_is_rejected(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="/etc/hostname"), encoding="utf-8"
    )
    manifest_path = _write_manifest(case_dir)
    with pytest.raises(AssetEvidenceError, match="禁止|越界"):
        inspect_asset_contract(manifest_path)


def test_expected_asset_mismatch_is_rejected(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="asset-b.ppm"), encoding="utf-8"
    )
    manifest_path = _write_manifest(case_dir, expected=9)
    with pytest.raises(AssetEvidenceError, match="count"):
        inspect_asset_contract(manifest_path)


def test_unknown_contract_is_rejected(case_dir):
    bad = json.loads(MANIFEST.read_text())
    bad["contracts"] = ["visual_similarity"]
    bad_path = case_dir / "bad-manifest.json"
    bad_path.write_text(json.dumps(bad))
    with pytest.raises(AssetEvidenceError, match="contract"):
        inspect_asset_contract(bad_path)


# --------------------------------------------------------------------------- #
# 反“灰度哈希/平均颜色单阈值”：共享背景主导全图度量，但主体裁决保持正确
# --------------------------------------------------------------------------- #
def test_whole_image_gray_metric_is_misleading_but_subject_evidence_holds():
    result = inspect_asset_contract(MANIFEST)
    assets = {asset["asset_id"]: asset for asset in result["assets"]}

    def mean_gray(asset):
        # 独立从原始 PPM 字节核算全图平均灰度，不依赖被测模块的主体掩码
        raw = (PROJECT_ROOT / asset["image_path"]).read_bytes()
        pixels = raw[asset["pixel_byte_offset"]:]
        return sum(pixels) / len(pixels)

    gray_a = mean_gray(assets["asset-1"])
    gray_b = mean_gray(assets["asset-2"])
    gray_c = mean_gray(assets["asset-3"])
    # 全图平均灰度上 A 与 C（共用模板）比 A 与裁切旋转后的 B 更接近：
    # 这正是灰度哈希/平均颜色单阈值会被误导的场景
    assert abs(gray_a - gray_c) < abs(gray_a - gray_b)
    # 而真实裁决：A/B 同家族，A/C 拒合并
    assert _edge(result, "asset-1", "asset-2")["accepted"] is True
    assert _rejection(result, "asset-1", "asset-3")[
        "reject_reason"
    ] == "subject_color_or_shape_mismatch"


# --------------------------------------------------------------------------- #
# CLI 与 markdown 报告
# --------------------------------------------------------------------------- #
def _cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "selregress", *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=90,
    )


def test_asset_evidence_cli_json_and_markdown():
    result = _cli(
        "asset-evidence",
        "--manifest", "fixtures/asset_contract/manifest.json",
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["license_decisions"]["exact_copy"]["total_fee"] == 30
    assert payload["license_decisions"]["derivative_family"]["total_fee"] == 20

    md_result = _cli(
        "asset-evidence",
        "--manifest", "fixtures/asset_contract/manifest.json",
        "--format", "markdown",
    )
    assert md_result.returncode == 0, md_result.stderr
    markdown = md_result.stdout
    assert "`exact_copy`" in markdown and "`derivative_family`" in markdown
    assert "**30**" in markdown and "**20**" in markdown
    assert "拒合并反例" in markdown


def test_asset_evidence_cli_rejects_traversal_with_exit_three(case_dir):
    (case_dir / "snapshot.html").write_text(
        TWO_CARD_HTML.format(src="../../selregress/util.py"), encoding="utf-8"
    )
    manifest_path = _write_manifest(case_dir)
    result = _cli(
        "asset-evidence",
        "--manifest", manifest_path.relative_to(PROJECT_ROOT).as_posix(),
    )
    assert result.returncode == 3
    error = json.loads(result.stderr)
    assert error["error"] == "ASSET_EVIDENCE_REJECTED"
