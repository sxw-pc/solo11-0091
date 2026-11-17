"""Validate bounded local table-contract input without producing joined records."""

from __future__ import annotations

import json
from pathlib import Path

from bs4 import BeautifulSoup

from .models import FieldSpec
from .matcher import match_nodes
from .util import PROJECT_ROOT, sha256_file


def inspect_table_contract(manifest_path: str | Path) -> dict[str, object]:
    manifest_file = Path(manifest_path).resolve()
    manifest_file.relative_to(PROJECT_ROOT.resolve())
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    snapshot = (PROJECT_ROOT / manifest["snapshot"]).resolve()
    snapshot.relative_to(PROJECT_ROOT.resolve())
    soup = BeautifulSoup(snapshot.read_text(encoding="utf-8"), "html.parser")
    tables = soup.select(manifest["table_selector"])
    if len(tables) != 1:
        raise ValueError("table_selector must identify one local table")
    table = tables[0]
    rows = [row for row in table.select("tr") if row.find_parent("table") is table and row.find_parent("thead") is None]
    if len(rows) != manifest["expected_top_level_rows"]:
        raise ValueError("top-level row count differs from manifest")
    fields = {}
    for key in ("group_selector", "item_selector", "amount_selector"):
        spec = FieldSpec(name=key, selectors=(manifest[key],))
        fields[key] = match_nodes(table, spec).count
    contracts = manifest["contracts"]
    if contracts != ["carry_rowspan", "explicit_only"]:
        raise ValueError("unknown table contract")
    return {
        "snapshot": manifest["snapshot"],
        "sha256": sha256_file(snapshot),
        "top_level_rows": len(rows),
        "field_hits": fields,
        "contracts": contracts,
        "records": None,
    }
