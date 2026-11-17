from selregress.svg_chart_evidence import inspect_svg_chart
from selregress.util import PROJECT_ROOT


def test_svg_chart_evidence_is_bounded_and_uninterpreted():
    result=inspect_svg_chart(PROJECT_ROOT/"fixtures/svg_chart/manifest.json")
    assert result["series_points_raw"]=="0,0 50,50"
    assert result["series_transform_chain"]==["translate(10,10)","scale(2,1)"]
    assert result["decorative_polyline_count"]==1
    assert [x["value"] for x in result["axes"]["y"]]==["100","80"]
    assert [x["price"] for x in result["machine_series"]["points"]]==["100","85"]
    assert result["rendered_points"] is None
    assert result["alert_decisions"] is None
