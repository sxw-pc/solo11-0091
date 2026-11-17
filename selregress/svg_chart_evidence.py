"""Inspect bounded offline SVG chart evidence without interpreting screen coordinates."""
from __future__ import annotations

import json
from pathlib import Path
from bs4 import BeautifulSoup
from .matcher import match_nodes
from .models import FieldSpec
from .util import PROJECT_ROOT, sha256_file


def inspect_svg_chart(manifest_path: str | Path) -> dict[str, object]:
    manifest_file=Path(manifest_path).resolve()
    manifest_file.relative_to(PROJECT_ROOT.resolve())
    manifest=json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest["contracts"] != ["rendered_authority","embedded_data_authority"]:
        raise ValueError("unknown chart contracts")
    snapshot=(PROJECT_ROOT/manifest["snapshot"]).resolve()
    snapshot.relative_to(PROJECT_ROOT.resolve())
    soup=BeautifulSoup(snapshot.read_text(encoding="utf-8"),"html.parser")
    chart=match_nodes(soup,FieldSpec(name="chart",selectors=(manifest["chart_selector"],)))
    script=match_nodes(soup,FieldSpec(name="machine",selectors=(manifest["machine_data_selector"],)))
    if chart.count!=1 or script.count!=1:
        raise ValueError("chart and embedded series must be unique")
    series=match_nodes(chart.nodes[0],FieldSpec(name="series",selectors=(manifest["series_selector"],)))
    if series.count!=1:
        raise ValueError("price series must be unique")
    machine=json.loads(script.nodes[0].get_text())
    axes={kind:[{"x":node.get("x"),"y":node.get("y"),"value":node.get("data-value") or node.get("data-day")} for node in chart.nodes[0].select(f"text.{kind}-axis")] for kind in ("x","y")}
    return {"snapshot":manifest["snapshot"],"sha256":sha256_file(snapshot),"drop_alert_threshold":manifest["drop_alert_threshold"],"contracts":manifest["contracts"],"series_points_raw":series.nodes[0].get("points"),"series_transform_chain":[node.get("transform") for node in reversed(list(series.nodes[0].parents)) if node.get("transform")],"series_clip_chain":[node.get("clip-path") for node in reversed(list(series.nodes[0].parents)) if node.get("clip-path")],"axes":axes,"decorative_polyline_count":len(chart.nodes[0].select("polyline.decoration")),"machine_series":machine,"rendered_points":None,"alert_decisions":None}
