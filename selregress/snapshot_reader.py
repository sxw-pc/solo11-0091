"""本地 HTML 快照发现、元数据与解析。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from .models import SnapshotMeta
from .util import InputError, PROJECT_ROOT, relative_posix, resolve_under_root, sha256_bytes

SNAPSHOT_NAME = re.compile(r"^(list|detail)-(\d{4}-\d{2})\.html$")


def capture_placeholder(version: str) -> str:
    """由快照版本标签生成确定性的采集时间占位。"""
    return f"{version}-01T00:00:00Z" if re.fullmatch(r"\d{4}-\d{2}", version) else ""


@dataclass(slots=True)
class LoadedSnapshot:
    meta: SnapshotMeta
    soup: Any


def scan_snapshots(snapshot_dir: Path, root: Path = PROJECT_ROOT) -> tuple[list[SnapshotMeta], list[str]]:
    snapshot_dir = resolve_under_root(snapshot_dir, root, "snapshot_dir")
    if not snapshot_dir.is_dir():
        raise InputError("SNAPSHOT_DIR_NOT_FOUND", f"快照目录不存在: {snapshot_dir}")
    metas: list[SnapshotMeta] = []
    warnings: list[str] = []
    for path in sorted(snapshot_dir.rglob("*.html")):
        name_match = SNAPSHOT_NAME.match(path.name)
        if not name_match:
            warnings.append(f"跳过无法识别文件名的快照: {relative_posix(path, root)}")
            continue
        site = path.parent.name
        page_type, version = name_match.groups()
        try:
            raw = path.read_bytes()
        except OSError as exc:
            warnings.append(f"跳过无法读取的快照 {relative_posix(path, root)}: {exc}")
            continue
        if not raw:
            warnings.append(f"跳过空快照: {relative_posix(path, root)}")
            continue
        metas.append(
            SnapshotMeta(
                site=site,
                page_type=page_type,
                version=version,
                path=relative_posix(path, root),
                sha256=sha256_bytes(raw),
                size=len(raw),
                captured_at=capture_placeholder(version),
            )
        )
    metas.sort(key=lambda item: (item.site, item.page_type, item.version, item.path))
    return metas, sorted(warnings)


def read_snapshot(
    path: Path,
    *,
    site: str,
    page_type: str,
    version: str,
    root: Path = PROJECT_ROOT,
) -> LoadedSnapshot:
    snapshot_path = resolve_under_root(path, root, "snapshot")
    if not snapshot_path.is_file():
        raise InputError("SNAPSHOT_NOT_FOUND", f"快照不存在: {path}")
    raw = snapshot_path.read_bytes()
    if not raw:
        raise InputError("S001_EMPTY_SNAPSHOT", f"快照为空: {relative_posix(snapshot_path, root)}")
    try:
        text = raw.decode("utf-8")
        warnings: list[str] = []
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
        warnings = ["S003_ENCODING_FALLBACK"]
    lowered = text.lower()
    if "<html" not in lowered:
        warnings.append("S002_UNSTRUCTURED_HTML")
    try:
        soup = BeautifulSoup(text, "html.parser")
    except Exception as exc:  # html.parser 对畸形输入通常宽容，仍防御性处理
        raise InputError("S004_PARSE_FAILED", f"HTML 解析失败: {exc}") from exc
    meta = SnapshotMeta(
        site=site,
        page_type=page_type,
        version=version,
        path=relative_posix(snapshot_path, root),
        sha256=sha256_bytes(raw),
        size=len(raw),
        captured_at=capture_placeholder(version),
        warnings=tuple(warnings),
    )
    return LoadedSnapshot(meta=meta, soup=soup)


def load_snapshots(snapshot_dir: Path, root: Path = PROJECT_ROOT) -> tuple[list[LoadedSnapshot], list[str]]:
    metas, warnings = scan_snapshots(snapshot_dir, root)
    loaded: list[LoadedSnapshot] = []
    for meta in metas:
        path = root / meta.path
        loaded.append(
            read_snapshot(
                path, site=meta.site, page_type=meta.page_type,
                version=meta.version, root=root,
            )
        )
    return loaded, warnings
