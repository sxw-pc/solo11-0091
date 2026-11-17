from bs4 import BeautifulSoup

from selregress.extractor import extract_field
from selregress.matcher import match_nodes
from selregress.models import FieldSpec


def run(html, *, type="string", extract="text", normalize=(), multi=False, separator=None, attribute=None):
    spec = FieldSpec(
        name="value", selectors=(".target",), field_type=type, extract=extract,
        normalize=tuple(normalize), multi=multi, separator=separator, attribute=attribute,
    )
    soup = BeautifulSoup(html, "html.parser")
    return extract_field(match_nodes(soup, spec), spec)


def test_text_collapse_and_lower_normalization():
    result = run('<p class="target">  HELLO   World </p>', normalize=("collapse_ws", "lower"))
    assert result.raw_values == ["HELLO   World"]
    assert result.normalized_value == "hello world"
    assert result.conversion_status == "ok"


def test_regex_replace_normalization():
    result = run(
        '<p class="target">NO-123</p>',
        normalize=[{"type": "regex_replace", "pattern": r"\d+", "replacement": "#"}],
    )
    assert result.normalized_value == "NO-#"


def test_decimal_strips_currency_and_thousands():
    result = run('<span class="target">￥9,900.50</span>', type="decimal", normalize=("trim",))
    assert result.normalized_value == "9900.5"
    assert result.conversion_status == "ok"


def test_integer_type_error_preserves_raw_value():
    result = run('<span class="target">十二</span>', type="integer")
    assert result.conversion_status == "type_error"
    assert result.raw_values == ["十二"]
    assert result.normalized_value == "十二"
    assert "integer" in result.error


def test_chinese_date_normalization():
    result = run('<time class="target">2024年2月8日</time>', type="date")
    assert result.normalized_value == "2024-02-08"
    result = run('<time class="target">2024/2/8</time>', type="date")
    assert result.normalized_value == "2024-02-08"


def test_boolean_values_and_failure():
    assert run('<i class="target">是</i>', type="boolean").normalized_value == "true"
    assert run('<i class="target">否</i>', type="boolean").normalized_value == "false"
    failed = run('<i class="target">也许</i>', type="boolean")
    assert failed.conversion_status == "type_error"


def test_url_relative_and_invalid():
    assert run('<a class="target" href="/x/1">x</a>', extract="href", type="url").normalized_value == "/x/1"
    assert run('<a class="target" href="javascript:bad">x</a>', extract="href", type="url").conversion_status == "type_error"


def test_attribute_and_image_src_extraction():
    attr = FieldSpec(name="v", selectors=(".target",), extract="attr", attribute="data-code")
    soup = BeautifulSoup('<span class="target" data-code=" C-01 "></span>', "html.parser")
    assert extract_field(match_nodes(soup, attr), attr).normalized_value == " C-01 "
    src = FieldSpec(name="v", selectors=("img.target",), extract="src")
    soup = BeautifulSoup('<img class="target" src="/local/a.png">', "html.parser")
    assert extract_field(match_nodes(soup, src), src).normalized_value == "/local/a.png"


def test_multi_values_list_and_separator():
    listed = run('<i class="target">甲</i><i class="target">乙</i>', multi=True)
    assert listed.normalized_value == ["甲", "乙"]
    joined = run('<i class="target">甲</i><i class="target">乙</i>', multi=True, separator=" | ")
    assert joined.normalized_value == "甲 | 乙"
    assert joined.match_count == 2


def test_html_fragment_and_empty_value():
    fragment = run('<div class="target"><p>示例 <b>甲</b></p></div>', extract="html", normalize=("trim",))
    assert fragment.normalized_value == "<p>示例 <b>甲</b></p>"
    empty = run('<div class="target">   </div>')
    assert empty.conversion_status == "empty"
    no_match = run('<div>nothing</div>')
    assert no_match.conversion_status == "no_match"
    assert no_match.normalized_value is None
