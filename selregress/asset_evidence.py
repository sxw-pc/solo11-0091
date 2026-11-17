"""Inspect local image assets selected by the snapshot recipe, without grouping them."""
from __future__ import annotations

import json
import re
from pathlib import Path
from bs4 import BeautifulSoup
from .matcher import match_nodes
from .models import FieldSpec
from .util import PROJECT_ROOT, sha256_file


def inspect_asset_contract(manifest_path: str | Path) -> dict[str, object]:
    manifest_file = Path(manifest_path).resolve()
    manifest_file.relative_to(PROJECT_ROOT.resolve())
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest["contracts"] != ["exact_copy", "derivative_family"]:
        raise ValueError("unknown asset contracts")
    snapshot = (PROJECT_ROOT / manifest["snapshot"]).resolve()
    snapshot.relative_to(PROJECT_ROOT.resolve())
    soup = BeautifulSoup(snapshot.read_text(encoding="utf-8"), "html.parser")
    assets = []
    for card in soup.select("article.asset"):
        sku = match_nodes(card, FieldSpec(name="sku", selectors=(".sku",)))
        image = match_nodes(card, FieldSpec(name="photo", selectors=("img.photo",), extract="src"))
        if sku.count != 1 or image.count != 1:
            raise ValueError("asset card must have one sku and one image")
        name = image.nodes[0].get("src")
        if not isinstance(name, str):
            raise ValueError("image src missing")
        image_file = (snapshot.parent / name).resolve()
        image_file.relative_to(snapshot.parent)
        raw = image_file.read_bytes()
        match = re.match(rb"P6\n([0-9]+) ([0-9]+)\n255\n", raw)
        if not match:
            raise ValueError("asset must be local PPM P6")
        width, height = int(match[1]), int(match[2])
        if len(raw) != match.end() + 3 * width * height:
            raise ValueError("PPM pixel bytes differ from header")
        assets.append({"sku": sku.nodes[0].get_text(" ", strip=True), "image_path": image_file.relative_to(PROJECT_ROOT).as_posix(), "width": width, "height": height, "sha256": sha256_file(image_file)})
    if len(assets) != manifest["expected_assets"] or len({a["sku"] for a in assets}) != len(assets):
        raise ValueError("asset count or sku identity mismatch")
    return {"snapshot": manifest["snapshot"], "snapshot_sha256": sha256_file(snapshot), "contracts": manifest["contracts"], "license_fee_per_unit": manifest["license_fee_per_unit"], "assets": assets, "license_decisions": None}
