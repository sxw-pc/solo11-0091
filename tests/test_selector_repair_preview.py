"""只读选择器修复预演的离线验收测试。

所有 fixture（合成配方、基线、两份 HTML 快照）都在 pytest `tmp_path` 中新建，
不声称仓库内已存在；测试不访问网络，也不修改原配方或快照。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from bs4 import BeautifulSoup

from selregress.config import load_config_for_planning
from selregress.ledger_query import LedgerQuery
from selregress.loader import load_bundle
from selregress.matcher import match_nodes
from selregress.models import FieldSpec
from selregress.repair import _load_baselines, _rehearse, compute_repair_plan, plan_selector_repair
from selregress.regression import outcome_fingerprint
from selregress.runner import execute
from selregress.suggest import enumerate_repair_candidates
from selregress.util import PROJECT_ROOT

SITE = "synth.invalid"


def _write_fixture(
    tmp_path: Path,
    *,
    fields: list[dict],
    snapshots: dict[str, str],
    baseline: dict | None = None,
    recipe_name: str = "synthetic_detail",
    fixed_time: str = "2024-03-01T00:00:00Z",
) -> tuple[Path, Path]:
    root = tmp_path
    snapshot_dir = root / "snapshots" / SITE
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    for version, html in sorted(snapshots.items()):
        (snapshot_dir / f"detail-{version}.html").write_text(html, encoding="utf-8")
    (root / "baselines").mkdir(parents=True, exist_ok=True)
    recipe: dict = {
        "schema_version": 1,
        "name": recipe_name,
        "site": SITE,
        "page_type": "detail",
        "fields": fields,
    }
    if baseline is not None:
        baseline_path = root / "baselines" / "fixture.baseline.json"
        baseline_path.write_text(
            json.dumps(baseline, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        recipe["baseline"] = "baselines/fixture.baseline.json"
    (root / "recipe.yaml").write_text(
        yaml.safe_dump(recipe, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    config = {
        "schema_version": 1,
        "name": "repair_fixture",
        "snapshot_dir": "snapshots",
        "recipes": ["recipe.yaml"],
        "ledger_path": "var/selregress.db",
        "runs_dir": "runs",
        "fixed_time": fixed_time,
    }
    config_path = root / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return root, config_path


def _tree_hashes(root: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.name.endswith(".db"):
            hashes[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return hashes


def _field(name: str, selector: str, **extra) -> dict:
    data = {"name": name, "selector": selector}
    data.update(extra)
    return data


def _baseline(values: dict[str, str]) -> dict:
    return {
        "schema_version": 1,
        "site": SITE,
        "page_type": "detail",
        "version": "2024-02",
        "generated_at": "2024-03-01T00:00:00Z",
        "fields": {
            name: {
                "normalized_value": value,
                "match_count": 1,
                "conversion_status": "ok",
                "score": 100,
            }
            for name, value in values.items()
        },
    }


PRODUCT_CODE_FIELDS = [
    _field("product_name", "h1.product-name", type="string", required=True),
    _field("brand", ".brand", type="string", required=True),
    _field("product_code", ".product-code", type="string", required=False),
    _field(
        "price",
        ".price",
        type="decimal",
        required=False,
        normalize=["trim", "strip_thousands"],
    ),
]

PRODUCT_CODE_BASELINE = _baseline(
    {
        "product_name": "示例乙设备",
        "brand": "示例乙机构",
        "product_code": "CODE-B-100",
        "price": "9900.5",
    }
)

PRODUCT_CODE_OLD = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<article class="product">
  <h1 class="product-name">示例乙设备</h1>
  <span class="brand">示例乙机构</span>
  <span class="product-code">CODE-B-100</span>
  <span class="price">9,900.50</span>
</article>
</body></html>
"""

PRODUCT_CODE_NEW = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<article class="product">
  <h1 class="product-name">示例乙设备</h1>
  <span class="brand">示例乙机构</span>
  <span class="product-sku">CODE-B-100</span>
  <span class="price">9,900.50</span>
</article>
</body></html>
"""


@pytest.fixture()
def product_code_case(tmp_path: Path):
    root, config_path = _write_fixture(
        tmp_path,
        fields=PRODUCT_CODE_FIELDS,
        snapshots={"2024-01": PRODUCT_CODE_OLD, "2024-02": PRODUCT_CODE_NEW},
        baseline=PRODUCT_CODE_BASELINE,
    )
    return root, load_config_for_planning(config_path)


def _plan(config, *, fields=("product_code",), scope="current"):
    return plan_selector_repair(
        config,
        recipe_name="synthetic_detail",
        fields=list(fields),
        scope=scope,
    )


# ---------------------------------------------------------------------------
# current / replay 的不同裁定
# ---------------------------------------------------------------------------


def test_current_replaces_selector_and_discloses_affected_old_version(product_code_case):
    root, config = product_code_case
    plan = _plan(config, scope="current")

    assert plan["feasible"] is True
    assert plan["replace_deliverable"] is True
    assert plan["failing_fields"] == ["product_code"]
    recommended = plan["recommended"]
    assert [item["action"] for item in recommended["actions"]] == ["replace"]
    assert recommended["actions"][0]["selectors"] == [".product-sku"]
    assert recommended["changed_field_count"] == 1
    assert recommended["added_selector_count"] == 1

    # current 只承诺最新快照；旧版受影响必须显式披露，而不是悄悄回归。
    affected = recommended["affected_snapshots"]
    assert len(affected) == 1
    assert affected[0]["field_name"] == "product_code"
    assert affected[0]["version"] == "2024-01"
    assert affected[0]["before_conversion_status"] == "ok"
    assert affected[0]["after_conversion_status"] == "no_match"
    assert affected[0]["scope_required"] is False

    # 建议分存在，但它不是交付证据。
    suggestion = recommended["actions"][0]["suggestion"]
    assert suggestion["score"] > 0
    assert suggestion["confidence_is_not_repair_evidence"] is True


def test_replay_rejects_replacement_and_chooses_fallback(product_code_case):
    root, config = product_code_case
    current = _plan(config, scope="current")
    replay = _plan(config, scope="replay")

    assert replay["feasible"] is True
    assert replay["replace_deliverable"] is False
    recommended = replay["recommended"]
    assert recommended["actions"][0]["action"] == "append"
    assert recommended["actions"][0]["selectors"] == [".product-code", ".product-sku"]
    assert recommended["affected_snapshots"] == []

    # 同一输入，两种范围对“替换是否可交付”给出不同结论。
    assert current["replace_deliverable"] is True
    assert replay["replace_deliverable"] is False
    assert current["plan_id"] != replay["plan_id"]

    replace_preview = next(
        item
        for item in replay["field_actions"]
        if item["action"] == "replace" and item["candidate_selector"] == ".product-sku"
    )
    assert replace_preview["target_scope_met"] is False
    assert any(
        conflict["kind"] == "scope_not_met"
        and conflict["version"] == "2024-01"
        and conflict["selector"] == ".product-sku"
        for conflict in replace_preview["conflicts"]
    )


# ---------------------------------------------------------------------------
# 无值 / 多命中 / 类型失败 / 多字段择优
# ---------------------------------------------------------------------------


MULTI_FIELDS = [
    _field("product_name", "h1.product-name", type="string", required=True),
    _field("product_code", ".product-code", type="string", required=False),
    _field(
        "price",
        ".price",
        type="decimal",
        required=False,
        normalize=["trim", "strip_thousands"],
    ),
    _field("category", ".category", type="string", required=False),
    _field("stock", ".stock", type="string", required=False),
]

MULTI_BASELINE = _baseline(
    {
        "product_name": "商品丙",
        "product_code": "CODE-C",
        "price": "1280",
        "category": "主分类",
        "stock": "7",
    }
)

MULTI_OLD = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<article class="product">
  <h1 class="product-name">商品丙</h1>
  <span class="product-code">CODE-C</span>
  <span class="price">1,280.00</span>
  <span class="price-value">1,280.00</span>
  <span class="category">主分类</span>
  <span class="category-main">主分类</span>
  <span class="stock">12</span>
  <span class="stock-value">12</span>
</article>
</body></html>
"""

MULTI_NEW = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<article class="product">
  <h1 class="product-name">商品丙</h1>
  <span class="product-sku">CODE-C</span>
  <span class="price">面议</span>
  <span class="price-value">1,280.00</span>
  <span class="category">主分类</span>
  <span class="category">副分类</span>
  <span class="category-main">主分类</span>
  <span class="stock">   </span>
  <span class="stock-value">7</span>
</article>
</body></html>
"""


@pytest.fixture()
def multi_failure_case(tmp_path: Path):
    root, config_path = _write_fixture(
        tmp_path,
        fields=MULTI_FIELDS,
        snapshots={"2024-01": MULTI_OLD, "2024-02": MULTI_NEW},
        baseline=MULTI_BASELINE,
    )
    return root, load_config_for_planning(config_path)


def test_multi_field_plan_covers_no_value_multi_match_and_type_error(multi_failure_case):
    root, config = multi_failure_case
    execution = execute(
        config,
        ledger_path=root / "base.db",
        runs_dir=root / "base_runs",
    )
    latest_flags = {
        item.field_name: set(item.flags)
        for item in execution.result.outcomes
        if item.version == "2024-02"
    }
    assert "no_match" in latest_flags["product_code"]
    assert "type_error" in latest_flags["price"]
    assert "multi_match" in latest_flags["category"]
    assert "empty_value" in latest_flags["stock"]

    plan = _plan(
        config,
        fields=("product_name", "product_code", "price", "category", "stock"),
        scope="replay",
    )
    assert plan["feasible"] is True
    # 已经达标的 product_name 不应被改动：最少改动字段数。
    assert plan["requested_fields"] == [
        "product_name", "product_code", "price", "category", "stock",
    ]
    assert plan["failing_fields"] == ["product_code", "price", "category", "stock"]
    recommended = plan["recommended"]
    assert recommended["changed_field_count"] == 4
    assert recommended["added_selector_count"] == 4
    actions = {item["field_name"]: item for item in recommended["actions"]}
    assert set(actions) == {"product_code", "price", "category", "stock"}
    # 稳定字段顺序：按配方声明顺序。
    assert [item["field_name"] for item in recommended["actions"]] == [
        "product_code", "price", "category", "stock",
    ]
    assert actions["product_code"]["action"] == "append"
    assert actions["product_code"]["selectors"] == [".product-code", ".product-sku"]
    assert actions["price"]["selectors"] == [".price-value"]
    assert actions["category"]["selectors"] == [".category-main"]
    assert actions["stock"]["selectors"] == [".stock-value"]
    for field_name in ("product_code", "price", "category", "stock"):
        details = recommended["target_results"][field_name]
        assert details["clean"] is True
        assert all(item["ok"] for item in details["snapshots"])
    assert plan["conflicts"] == []


def test_recommended_plan_does_not_change_untargeted_results(multi_failure_case):
    root, config = multi_failure_case
    bundle = load_bundle(config)
    baselines = _load_baselines(config, bundle)
    base_execution = execute(
        config, ledger_path=root / "base.db", runs_dir=root / "base_runs"
    )
    plan = _plan(config, fields=("product_code", "price"), scope="replay")
    assert plan["feasible"] is True

    recipe = next(item for item in bundle.recipes if item.name == "synthetic_detail")
    patched: dict[tuple[str, str], FieldSpec] = {}
    for action in plan["recommended"]["actions"]:
        spec = next(item for item in recipe.fields if item.name == action["field_name"])
        patched[(recipe.name, spec.name)] = replace(
            spec, selectors=tuple(action["selectors"])
        )
    rehearsal = _rehearse(config, bundle, baselines, patched)
    base_map = {
        (item.recipe_name, item.version, item.snapshot_path, item.field_name): item
        for item in base_execution.result.outcomes
    }
    for outcome in rehearsal.outcomes:
        if outcome.field_name in {"product_code", "price"}:
            continue
        old = base_map[
            (outcome.recipe_name, outcome.version, outcome.snapshot_path, outcome.field_name)
        ]
        assert outcome_fingerprint(outcome) == outcome_fingerprint(old)


# ---------------------------------------------------------------------------
# 候选跨字段误采 / 业务对象变化 / 基线冲突 / 不可行证据
# ---------------------------------------------------------------------------


BAD_FIELDS = [
    _field("product_name", ".primary .product-name", type="string", required=True),
    _field("price", ".primary .price", type="decimal", required=False),
    _field("product_code", ".product-code", type="string", required=False),
]

BAD_BASELINE = _baseline(
    {
        "product_name": "商品甲",
        "price": "10",
        "product_code": "CODE-A",
    }
)

BAD_OLD = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<main class="listing">
  <article class="product primary">
    <h1 class="product-name">商品甲</h1>
    <span class="product-code">CODE-A</span>
    <span class="price">10.00</span>
  </article>
  <article class="product secondary">
    <h1 class="product-name">商品乙</h1>
    <span class="product-cost">99.00</span>
    <span class="price">99.00</span>
  </article>
</main>
</body></html>
"""

BAD_NEW = """<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>
<main class="listing">
  <article class="product primary">
    <h1 class="product-name">商品甲</h1>
    <span class="price">10.00</span>
  </article>
  <article class="product secondary">
    <h1 class="product-name">商品乙</h1>
    <span class="product-cost">99.00</span>
    <span class="price">99.00</span>
  </article>
</main>
</body></html>
"""


@pytest.fixture()
def mis_capture_case(tmp_path: Path):
    root, config_path = _write_fixture(
        tmp_path,
        fields=BAD_FIELDS,
        snapshots={"2024-01": BAD_OLD, "2024-02": BAD_NEW},
        baseline=BAD_BASELINE,
        recipe_name="bad_detail",
    )
    return root, load_config_for_planning(config_path)


def test_lookalike_candidate_is_not_feasible_even_if_it_fixes_no_match(mis_capture_case):
    root, config = mis_capture_case
    plan = plan_selector_repair(
        config,
        recipe_name="bad_detail",
        fields=["product_code"],
        scope="current",
    )
    assert plan["feasible"] is False
    assert plan["recommended"] is None
    assert plan["replace_deliverable"] is False

    preview = next(
        item
        for item in plan["field_actions"]
        if item["action"] == "replace" and item["candidate_selector"] == ".product-cost"
    )
    # 它确实让 no_match 消失（范围达标），但改变了业务对象，不能标可交付。
    assert preview["target_scope_met"] is True
    assert preview["feasible_for_field"] is False

    kinds = {item["kind"] for item in preview["conflicts"]}
    assert "baseline_value_conflict" in kinds
    assert "business_object_mismatch" in kinds
    baseline_conflict = next(
        item for item in preview["conflicts"] if item["kind"] == "baseline_value_conflict"
    )
    # 该候选实际抽到的是另一张卡片的价格，而不是商品编码。
    assert baseline_conflict["actual"] == "99.00"
    assert baseline_conflict["expected"] == "CODE-A"
    mismatch = next(
        item
        for item in preview["conflicts"]
        if item["kind"] == "business_object_mismatch" and item["version"] == "2024-02"
    )
    assert mismatch["field_name"] == "product_code"
    assert mismatch["snapshot_path"].endswith("detail-2024-02.html")
    assert mismatch["selector"] == ".product-cost"
    assert mismatch["expected"][0]["record_index"] == 0
    assert mismatch["actual"]["record_index"] == 1

    # 全局冲突证据覆盖字段 / 快照 / 选择器三级，可供人工定位。
    assert plan["conflicts"]
    for conflict in plan["conflicts"]:
        assert conflict["field_name"]
        assert conflict["kind"]
        assert "message" in conflict
    assert any(item["snapshot_path"] for item in plan["conflicts"])
    assert any(item["selector"] for item in plan["conflicts"])


def test_baseline_conflict_blocks_same_object_candidate(mis_capture_case):
    root, config = mis_capture_case
    plan = plan_selector_repair(
        config,
        recipe_name="bad_detail",
        fields=["product_code"],
        scope="current",
    )
    preview = next(
        item
        for item in plan["field_actions"]
        if item["action"] == "replace" and item["candidate_selector"] == ".product-name"
    )
    conflict = next(
        item for item in preview["conflicts"] if item["kind"] == "baseline_value_conflict"
    )
    assert conflict["field_name"] == "product_code"
    assert conflict["expected"] == "CODE-A"
    assert conflict["selector"] == ".product-name"
    assert conflict["snapshot_path"].endswith("detail-2024-02.html")


def test_cross_field_node_overlap_is_reported(mis_capture_case):
    root, config = mis_capture_case
    plan = plan_selector_repair(
        config,
        recipe_name="bad_detail",
        fields=["product_code"],
        scope="current",
    )
    overlaps = [
        item
        for item in plan["conflicts"]
        if item["kind"] == "cross_field_node_overlap"
        and item["evidence"].get("other_field") == "product_name"
    ]
    assert overlaps
    assert any(
        item["selector"] in {".product-name", "h1.product-name"} for item in overlaps
    )
    # 每条证据都带字段、快照、选择器与重叠关系。
    for item in overlaps:
        assert item["field_name"] == "product_code"
        assert item["snapshot_path"].endswith(".html")
        assert item["selector"]
        assert item["evidence"]["relation"] in {
            "same_node", "candidate_is_ancestor", "candidate_is_descendant",
        }


# ---------------------------------------------------------------------------
# 重复预演稳定 / SQLite 追溯 / 只读
# ---------------------------------------------------------------------------


def test_repeated_preview_is_stable_and_traceable(product_code_case):
    root, config = product_code_case
    before = _tree_hashes(root)
    first = _plan(config, scope="replay")
    second = _plan(config, scope="replay")
    after = _tree_hashes(root)

    # 原配方、基线与快照均只读；新增的只有台账与报告。
    for relative, digest in before.items():
        assert after.get(relative) == digest, relative

    for key in ("plan_id", "feasible", "replace_deliverable", "recommended", "conflicts", "field_actions"):
        assert first[key] == second[key], key

    ledger_path = config.ledger_path
    ledger = LedgerQuery(ledger_path, config.root)
    runs = ledger.list_runs(limit=20)
    plans = ledger.list_repair_plans(limit=20)
    assert len(runs) >= 2
    assert len(plans) >= 2
    assert {item["plan_id"] for item in plans} == {first["plan_id"]}
    assert plans[0]["base_run_id"] != plans[1]["base_run_id"]
    assert plans[0]["feasible"] is True
    assert plans[0]["replace_deliverable"] is False
    stored = ledger.get_repair_plan(first["plan_id"])
    assert stored is not None
    assert stored["plan"]["recommended"]["actions"][0]["action"] == "append"
    actions = ledger.query_repair_actions(first["plan_id"])
    assert actions
    assert {item["action"] for item in actions} <= {"replace", "append", "abandon"}
    assert any(item["candidate_selector"] == ".product-sku" for item in actions)

    # 旧运行同样可在台账中追溯：运行、配方、快照与字段结果都在。
    run = ledger.get_run(first["base_run"]["run_id"])
    assert run is not None
    assert run["status"] in {"pass", "warn", "fail"}
    assert ledger.list_recipes(run["id"])
    assert len(ledger.list_snapshots(run["id"])) == 2
    assert ledger.query_fields(first["base_run"]["run_id"], field_name="product_code")

    # 计划报告作为独立审计产物写入旧运行目录。
    artifacts = first["artifacts"]
    assert (root / artifacts["files"]["json"]).is_file()
    assert (root / artifacts["files"]["markdown"]).is_file()


def test_cli_replay_plan_on_isolated_fixture(product_code_case):
    root, config = product_code_case
    before = _tree_hashes(root)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if item
    )
    result = subprocess.run(
        [
            sys.executable, "-m", "selregress", "plan-selector-repair",
            "--config", str(config.config_path),
            "--recipe", "synthetic_detail",
            "--field", "product_code",
            "--scope", "replay",
            "--fixed-time", "2024-03-01T00:00:00Z",
        ],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180, env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["command"] == "plan-selector-repair"
    summary = payload["summary"]
    assert summary["feasible"] is True
    assert summary["replace_deliverable"] is False
    assert summary["recommended"]["actions"][0]["action"] == "append"
    assert summary["recommended"]["actions"][0]["selectors"] == [
        ".product-code", ".product-sku",
    ]
    assert summary["conflict_count"] == 0
    assert payload["plan"]["scope"] == "replay"
    assert payload["plan"]["scope_semantics"]["required_snapshots"]
    assert (root / summary["artifacts"]["files"]["json"]).is_file()

    after = _tree_hashes(root)
    for relative, digest in before.items():
        assert after.get(relative) == digest, relative


def test_cli_current_and_replay_disagree_on_replace(product_code_case):
    root, config = product_code_case
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(PROJECT_ROOT), env.get("PYTHONPATH", "")) if item
    )

    def run(scope: str) -> dict:
        completed = subprocess.run(
            [
                sys.executable, "-m", "selregress", "plan-selector-repair",
                "--config", str(config.config_path),
                "--recipe", "synthetic_detail",
                "--field", "product_code",
                "--scope", scope,
                "--fixed-time", "2024-03-01T00:00:00Z",
            ],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=180, env=env,
        )
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)["summary"]

    current = run("current")
    replay = run("replay")
    assert current["recommended"]["actions"][0]["action"] == "replace"
    assert current["replace_deliverable"] is True
    assert replay["recommended"]["actions"][0]["action"] == "append"
    assert replay["replace_deliverable"] is False
    assert any(
        item["version"] == "2024-01"
        for item in current["recommended"]["affected_snapshots"]
    )
    assert replay["recommended"]["affected_snapshots"] == []


def test_unknown_field_or_recipe_is_structured_error(product_code_case):
    root, config = product_code_case
    from selregress.util import InputError

    with pytest.raises(InputError) as caught:
        plan_selector_repair(
            config, recipe_name="synthetic_detail", fields=["missing"], scope="current"
        )
    assert caught.value.code == "FIELD_NOT_FOUND"
    with pytest.raises(InputError) as caught:
        plan_selector_repair(
            config, recipe_name="missing_recipe", fields=["product_code"], scope="current"
        )
    assert caught.value.code == "RECIPE_NOT_FOUND"


# ---------------------------------------------------------------------------
# 纯内存计算不落盘（除调用方显式写台账）
# ---------------------------------------------------------------------------


def test_compute_plan_is_in_memory_and_deterministic(product_code_case):
    root, config = product_code_case
    bundle = load_bundle(config)
    base = execute(config, ledger_path=root / "base.db", runs_dir=root / "base_runs")
    before = _tree_hashes(root)
    first = compute_repair_plan(
        config,
        bundle,
        base.result,
        recipe_name="synthetic_detail",
        fields=["product_code"],
        scope="replay",
    )
    second = compute_repair_plan(
        config,
        bundle,
        base.result,
        recipe_name="synthetic_detail",
        fields=["product_code"],
        scope="replay",
    )
    after = _tree_hashes(root)
    assert first == second
    for relative, digest in before.items():
        assert after.get(relative) == digest
    # 结果排序与序列化稳定。
    assert json.dumps(first, ensure_ascii=False, sort_keys=True) == json.dumps(
        second, ensure_ascii=False, sort_keys=True
    )


def test_candidate_score_is_advisory_not_success():
    """高建议分不能替代真实抽取；mis-capture 用例中 .product-cost 分数不低。"""

    spec = FieldSpec(name="product_code", selectors=(".product-code",))
    soup = BeautifulSoup(BAD_NEW, "html.parser")
    selection = match_nodes(soup, spec)
    candidates = enumerate_repair_candidates(soup, spec, selection)
    target = next(item for item in candidates if item["selector"] == ".product-cost")
    assert target["score"] > 0
    assert target["confidence"] in {"low", "medium", "high"}
    assert "不得自动写回" in target["risk"]


def test_suggest_candidates_drive_real_matcher_and_extractor(product_code_case):
    """建议候选必须真正驱动 matcher/extractor，而不是停留在评分层。"""

    from bs4 import BeautifulSoup

    from selregress.extractor import extract_field
    from selregress.matcher import match_nodes

    spec = FieldSpec(name="product_code", selectors=(".product-code",))
    soup = BeautifulSoup(PRODUCT_CODE_NEW, "html.parser")
    selection = match_nodes(soup, spec)
    assert selection.count == 0

    candidates = enumerate_repair_candidates(soup, spec, selection)
    selectors = [item["selector"] for item in candidates]
    assert ".product-sku" in selectors

    # 用候选真实重跑 matcher → extractor，得到可交付值。
    candidate_selection = match_nodes(soup, spec, selectors=(".product-sku",))
    extraction = extract_field(candidate_selection, spec)
    assert extraction.conversion_status == "ok"
    assert extraction.normalized_value == "CODE-B-100"
    assert extraction.used_selector == ".product-sku"

    # 相反，评分相近但采到标题的候选真实抽取为标题文本，不能被当作修复。
    title_selection = match_nodes(soup, spec, selectors=(".product-name",))
    title_extraction = extract_field(title_selection, spec)
    assert title_extraction.normalized_value == "示例乙设备"
