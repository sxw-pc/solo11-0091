from selregress.asset_evidence import inspect_asset_contract
from selregress.util import PROJECT_ROOT


def test_local_asset_fixture():
    result = inspect_asset_contract(PROJECT_ROOT / "fixtures/asset_contract/manifest.json")
    assets = result["assets"]
    assert [x["sku"] for x in assets] == ["sku-red-1", "sku-red-2", "sku-blue-1"]
    assert [(x["width"],x["height"]) for x in assets] == [(64,64),(52,52),(64,64)]
    assert len({x["sha256"] for x in assets}) == 3
    assert result["license_decisions"] is None
