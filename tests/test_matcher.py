from bs4 import BeautifulSoup

from selregress.matcher import match_nodes
from selregress.models import FieldSpec


def field(selectors, extract="text", attribute=None):
    return FieldSpec(
        name="sample", selectors=tuple(selectors), extract=extract, attribute=attribute,
    )


def test_single_match():
    soup = BeautifulSoup('<h1 class="title">标题甲</h1>', "html.parser")
    selection = match_nodes(soup, field(["h1.title"]))
    assert selection.count == 1
    assert selection.used_selector == "h1.title"
    assert selection.ambiguous is False
    assert selection.selector_hits[0]["count"] == 1


def test_fallback_to_second_selector():
    soup = BeautifulSoup('<div class="headline">备用标题</div>', "html.parser")
    selection = match_nodes(soup, field(["h1.title", ".headline"]))
    assert selection.count == 1
    assert selection.used_selector == ".headline"
    assert selection.ambiguous is False


def test_multiple_selectors_with_different_values_are_ambiguous():
    soup = BeautifulSoup(
        '<h1 class="title">新版</h1><div class="headline">旧版</div>', "html.parser"
    )
    selection = match_nodes(soup, field(["h1.title", ".headline"]))
    assert selection.count == 1
    assert selection.used_selector == "h1.title"
    assert selection.ambiguous is True
    assert "不一致" in selection.ambiguity_detail


def test_multiple_selectors_with_same_value_are_not_ambiguous():
    soup = BeautifulSoup(
        '<h1 class="title">同名</h1><div class="headline">同名</div>', "html.parser"
    )
    selection = match_nodes(soup, field(["h1.title", ".headline"]))
    assert selection.ambiguous is False


def test_multi_match_and_no_match_counts():
    soup = BeautifulSoup('<span class="tag">甲</span><span class="tag">乙</span>', "html.parser")
    selection = match_nodes(soup, field([".tag"]))
    assert selection.count == 2
    assert selection.selector_hits[0]["samples"] == ["甲", "乙"]
    missing = match_nodes(soup, field([".missing"]))
    assert missing.count == 0
    assert missing.used_selector == ".missing"
