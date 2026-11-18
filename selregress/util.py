"""共享工具：路径边界、确定性序列化、时间与指纹。"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SEVERITY_ORDER = {"fail": 0, "warn": 1, "info": 2, "pass": 3}


class SelregressError(Exception):
    """项目内可预期的结构化错误。"""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class InputError(SelregressError):
    """配置或输入不合法，对应 CLI 退出码 3。"""


def resolve_under_root(raw: str | Path, root: Path, label: str = "path") -> Path:
    """解析路径并拒绝逃出项目根目录的输入。"""
    root = root.resolve()
    # A Windows drive or UNC path is never a relative child of ``root``, even
    # on POSIX where ``Path`` would otherwise treat "C:/x" as a relative name.
    if PureWindowsPath(str(raw)).drive:
        raise InputError("PATH_OUTSIDE_ROOT", f"{label} 超出项目根目录: {raw}")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise InputError("PATH_OUTSIDE_ROOT", f"{label} 超出项目根目录: {raw}") from exc
    return resolved


def relative_posix(path: Path, root: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def canonical_json_bytes(value: Any) -> bytes:
    text = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return text.encode("utf-8")


def canonical_digest(value: Any) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def stable_json_text(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, indent=2, default=str
    ) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def normalized_fixed_time(value: str | None) -> tuple[str, bool]:
    """返回 UTC ISO 秒级时间与是否固定。"""
    if value is None:
        now = datetime.now(timezone.utc).replace(microsecond=0)
        return now.isoformat().replace("+00:00", "Z"), False
    raw = value.strip()
    if not raw:
        raise InputError("INVALID_FIXED_TIME", "fixed_time 不能为空")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InputError("INVALID_FIXED_TIME", f"无法解析时间: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc).replace(microsecond=0)
    return parsed.isoformat().replace("+00:00", "Z"), True


def run_stamp(generated_at: str) -> str:
    return re.sub(r"[^0-9TZ]", "", generated_at)


def safe_run_id_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-")
    return cleaned or "run"


def severity_rank(value: str) -> int:
    return SEVERITY_ORDER.get(value, 99)


def clamp_score(value: int) -> int:
    return max(0, min(100, int(value)))


def truncate(text: str, limit: int = 240) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def as_posix_relpath(value: str) -> str:
    return value.replace("\\", "/")
