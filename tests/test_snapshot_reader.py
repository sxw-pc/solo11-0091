from pathlib import Path

import pytest

from selregress.snapshot_reader import load_snapshots, read_snapshot, scan_snapshots
from selregress.util import PROJECT_ROOT, InputError

SNAPSHOTS = PROJECT_ROOT / "fixtures/snapshots"


def test_scan_finds_all_snapshots_and_metadata():
    metas, warnings = scan_snapshots(SNAPSHOTS)
    assert len(metas) == 6
    assert warnings == []
    first = metas[0]
    assert first.site == "site-a.invalid"
    assert first.page_type in {"list", "detail"}
    assert len(first.sha256) == 64
    assert first.path.startswith("fixtures/snapshots/")


def test_read_snapshot_and_fingerprint_stability():
    path = SNAPSHOTS / "site-a.invalid/detail-2024-01.html"
    first = read_snapshot(path, site="site-a.invalid", page_type="detail", version="2024-01")
    second = read_snapshot(path, site="site-a.invalid", page_type="detail", version="2024-01")
    assert first.meta.sha256 == second.meta.sha256
    assert first.meta.size == path.stat().st_size
    assert first.meta.warnings == ()
    assert first.meta.captured_at == "2024-01-01T00:00:00Z"
    assert first.soup.select_one("h1.product-name").get_text(strip=True) == "示例甲型号 A"


def test_empty_snapshot_is_structured_error():
    path = PROJECT_ROOT / "fixtures/bad/empty_snapshot.html"
    with pytest.raises(InputError) as caught:
        read_snapshot(path, site="site-a.invalid", page_type="detail", version="bad")
    assert caught.value.code == "S001_EMPTY_SNAPSHOT"


def test_malformed_html_parses_with_warning():
    path = PROJECT_ROOT / "fixtures/bad/broken_snapshot.html"
    loaded = read_snapshot(path, site="site-a.invalid", page_type="detail", version="bad")
    assert "S002_UNSTRUCTURED_HTML" in loaded.meta.warnings
    assert loaded.soup.select_one("h1.product-name") is not None


def test_scan_skips_bad_names_and_empty_files(workspace):
    site = workspace / "snapshots/site-x.invalid"
    site.mkdir(parents=True)
    (site / "detail-2024-01.html").write_text("<html><body><h1>x</h1></body></html>", encoding="utf-8")
    (site / "not-a-snapshot.html").write_text("ignored", encoding="utf-8")
    (site / "detail-2024-02.html").write_text("", encoding="utf-8")
    metas, warnings = scan_snapshots(workspace / "snapshots", PROJECT_ROOT)
    assert len(metas) == 1
    assert any("无法识别文件名" in item for item in warnings)
    assert any("空快照" in item for item in warnings)


def test_load_snapshots_returns_sorted_objects(shared_config):
    loaded, warnings = load_snapshots(shared_config.snapshot_dir, shared_config.root)
    assert warnings == []
    keys = [(item.meta.site, item.meta.page_type, item.meta.version) for item in loaded]
    assert keys == sorted(keys)


def test_snapshot_outside_root_is_rejected():
    outside = PROJECT_ROOT.parent / "outside-snapshot.html"
    with pytest.raises(InputError) as caught:
        read_snapshot(outside, site="x.invalid", page_type="detail", version="v", root=PROJECT_ROOT)
    assert caught.value.code == "PATH_OUTSIDE_ROOT"
