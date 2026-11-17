from bs4 import BeautifulSoup

from selregress.config import DEFAULT_RULES
from selregress.diagnostics import diagnose_field
from selregress.extractor import extract_field
from selregress.matcher import match_nodes
from selregress.models import FieldSpec, Recipe, SnapshotMeta
from selregress.suggest import analyze_failure
from selregress.util import PROJECT_ROOT, sha256_file


def outcome(execution, recipe, field, version="2024-02"):
    for item in execution.result.outcomes:
        if item.recipe_name == recipe and item.field_name == field and item.version == version:
            return item
    raise AssertionError(f"missing outcome {recipe}.{field}")


def findings(execution, code):
    return [item for item in execution.result.findings if item.code == code]


def test_no_match_flag_finding_and_score(shared_execution):
    item = outcome(shared_execution, "site_a_detail", "stock")
    assert "no_match" in item.flags
    assert item.score == 60
    assert item.status == "warn"
    finding = [x for x in findings(shared_execution, "no_match") if x.field_name == "stock"][0]
    assert finding.suggestions
    assert "parent_chain" in finding.evidence


def test_multi_match_is_flagged_for_single_value(shared_execution):
    item = outcome(shared_execution, "site_a_list", "featured_category")
    assert "multi_match" in item.flags
    assert item.extraction.match_count == 2
    assert item.score == 80
    suggestion = findings(shared_execution, "multi_match")[0].suggestions[0]
    assert len(findings(shared_execution, "multi_match")[0].suggestions) <= 5
    assert suggestion["confidence"] in {"high", "medium", "low"}


def test_type_error_keeps_raw_value(shared_execution):
    item = outcome(shared_execution, "site_a_detail", "price")
    assert "type_error" in item.flags
    assert item.extraction.raw_values == ["面议"]
    assert item.extraction.normalized_value == "面议"
    assert item.score == 75


def test_ambiguous_selector(shared_execution):
    item = outcome(shared_execution, "site_b_detail", "title")
    assert "ambiguous_selector" in item.flags
    assert item.extraction.used_selector == "h1.title"
    assert item.score == 85
    assert findings(shared_execution, "ambiguous_selector")


def test_version_drift_pairs_and_field_path(shared_execution):
    item = outcome(shared_execution, "site_a_detail", "stock")
    assert "version_drift" in item.flags
    drift = [x for x in findings(shared_execution, "version_drift") if x.field_name == "stock"][0]
    assert drift.evidence["version_pair"] == ["2024-01", "2024-02"]
    assert drift.evidence["field_path"] == "site-a.invalid.detail.stock"


def test_empty_value_is_diagnosed():
    spec = FieldSpec(name="empty", selectors=(".target",), required=True)
    snapshot = SnapshotMeta(
        site="site-a.invalid", page_type="detail", version="v1",
        path="fixtures/x.html", sha256="0" * 64, size=1,
    )
    recipe = Recipe(
        name="r", site="site-a.invalid", page_type="detail", fields=(spec,),
        path="configs/recipes/x.yaml", sha256="0" * 64,
    )
    soup = BeautifulSoup('<div class="target">   </div>', "html.parser")
    selection = match_nodes(soup, spec)
    extraction = extract_field(selection, spec)
    item, found = diagnose_field(
        recipe=recipe, snapshot=snapshot, spec=spec, selection=selection,
        extraction=extraction, soup=soup, rules=DEFAULT_RULES,
    )
    assert item.score == 70
    assert found[0].code == "empty_value"
    assert found[0].severity == "fail"


def test_candidate_suggestions_are_deterministic_and_read_only():
    path = PROJECT_ROOT / "fixtures/snapshots/site-a.invalid/list-2024-02.html"
    before = sha256_file(path)
    soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
    spec = FieldSpec(
        name="date", selectors=(".featured .card-date",), field_type="date", required=False
    )
    selection = match_nodes(soup, spec)
    first_evidence, first = analyze_failure(soup, spec, selection)
    second_evidence, second = analyze_failure(soup, spec, selection)
    assert first == second
    assert first_evidence == second_evidence
    assert 1 <= len(first) <= 5
    assert first[0]["selector"] == "time.card-time"
    assert all("不得自动写回" in item["risk"] for item in first)
    assert sha256_file(path) == before
