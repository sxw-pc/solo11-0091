"""命令行入口：demo / run / serve / ledger / query-release。"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Sequence

from .config import load_config
from .ledger import Ledger
from .runner import execute
from .util import InputError, SelregressError, resolve_under_root, stable_json_text

DEFAULT_CONFIG = "configs/demo.yaml"


class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise InputError("CLI_ARGUMENT", message)


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=DEFAULT_CONFIG, help="YAML 配置路径")
    parser.add_argument("--fixed-time", default=None, help="固定 UTC 时间，例如 2024-03-01T00:00:00Z")
    parser.add_argument("--db", default=None, help="覆盖 SQLite 台账路径")
    parser.add_argument("--out-dir", default=None, help="覆盖 runs 输出目录")
    parser.add_argument("--fail-on-warn", action="store_true", help="warn 时返回退出码 1")


def build_parser() -> argparse.ArgumentParser:
    parser = CliParser(prog="python -m selregress", description="择约回归台 CLI")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=CliParser)
    demo = sub.add_parser("demo", help="使用仓库 fixture 跑完整演示")
    _common(demo)
    run = sub.add_parser("run", help="按配置执行一次回归")
    _common(run)
    serve = sub.add_parser("serve", help="启动只读复核台")
    serve.add_argument("--config", default=DEFAULT_CONFIG)
    serve.add_argument("--port", type=int, default=5000)
    ledger = sub.add_parser("ledger", help="导出台账摘要")
    ledger.add_argument("--latest", action="store_true", help="输出最近一次运行摘要")
    ledger.add_argument("--config", default=DEFAULT_CONFIG)
    ledger.add_argument("--db", default=None)
    release = sub.add_parser(
        "query-release", help="对本地合成聚合查询卡按事前合同产出内部收据与公开卡集合")
    release.add_argument("--snapshot", required=True, help="本地聚合查询 HTML 快照")
    release.add_argument("--contract", required=True,
                         choices=["independent_card", "composition_safe"],
                         help="事前发布合同")
    release.add_argument("--version", default=None, help="快照版本标签，默认取文件名")
    release.add_argument("--out", default=None, help="将内部收据写入该本地 JSON 路径")
    release.add_argument("--public-out", default=None, help="将公开卡集合写入该本地 JSON 路径")
    release.add_argument("--root", default=None, help="路径根目录，默认项目根目录")
    verify = sub.add_parser(
        "query-verify", help="用绑定快照复核内部查询发布收据")
    verify.add_argument("--snapshot", required=True, help="本地聚合查询 HTML 快照")
    verify.add_argument("--receipt", required=True, help="内部收据 JSON 路径")
    verify.add_argument("--contract", default=None,
                        choices=["independent_card", "composition_safe"],
                        help="期望的事前发布合同")
    verify.add_argument("--version", default=None, help="快照版本标签，默认取文件名")
    verify.add_argument("--root", default=None, help="路径根目录，默认项目根目录")
    return parser


def _print_json(value: object) -> None:
    sys.stdout.write(stable_json_text(value))
    sys.stdout.flush()


def _run_command(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    execution = execute(
        config,
        fixed_time=args.fixed_time,
        ledger_path=Path(args.db) if args.db else None,
        runs_dir=Path(args.out_dir) if args.out_dir else None,
        fail_on_warn=bool(args.fail_on_warn),
    )
    _print_json(
        {
            "command": args.command,
            "run": {
                "run_id": execution.result.run_id,
                "status": execution.result.status,
                "exit_code": execution.result.exit_code,
                "generated_at": execution.result.generated_at,
                "summary": execution.result.summary,
                "reports": execution.artifacts["files"],
            },
            "warnings": execution.warnings,
        }
    )
    return execution.result.exit_code


def _serve_command(args: argparse.Namespace) -> int:
    from .server import create_app

    config = load_config(args.config)
    Ledger(config.ledger_path, config.root).initialize()
    app = create_app(config)
    print(f"择约回归台只读复核台: http://127.0.0.1:{args.port}/")
    print(f"台账: {config.ledger_path.name} · 按 Ctrl+C 停止")
    app.run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0


def _snapshot_version(path: Path) -> str:
    match = re.search(r"-(\d{4}-\d{2})\.html$", path.name)
    if match is None:
        raise InputError("CLI_ARGUMENT", f"无法从文件名推断快照版本: {path.name}")
    return match.group(1)


def _query_release_command(args: argparse.Namespace) -> int:
    from .query_privacy_evidence import inspect_query_snapshot
    from .query_release import decide_query_release, public_release_artifact
    from .util import PROJECT_ROOT, atomic_write_text

    root = Path(args.root).resolve() if args.root else PROJECT_ROOT
    snapshot = Path(args.snapshot)
    if not snapshot.is_absolute():
        snapshot = root / snapshot
    evidence = inspect_query_snapshot(
        snapshot, version=args.version or _snapshot_version(Path(args.snapshot)), root=root)
    receipt = decide_query_release(evidence, args.contract)
    public = public_release_artifact(receipt, expected_contract=args.contract)
    if args.out:
        out = resolve_under_root(args.out, root, "out")
        atomic_write_text(out, stable_json_text(receipt))
    if args.public_out:
        public_out = resolve_under_root(args.public_out, root, "public_out")
        atomic_write_text(public_out, stable_json_text(public))
    _print_json({
        "command": "query-release",
        "internal_receipt": receipt,
        "public_release": public,
    })
    return 0


def _query_verify_command(args: argparse.Namespace) -> int:
    import json

    from .query_privacy_evidence import inspect_query_snapshot
    from .query_release import verify_query_receipt
    from .util import PROJECT_ROOT

    root = Path(args.root).resolve() if args.root else PROJECT_ROOT
    snapshot = Path(args.snapshot)
    if not snapshot.is_absolute():
        snapshot = root / snapshot
    receipt_path = resolve_under_root(args.receipt, root, "receipt")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError("QUERY_RECEIPT_UNREADABLE", f"无法读取内部收据: {exc}") from exc
    evidence = inspect_query_snapshot(
        snapshot, version=args.version or _snapshot_version(Path(args.snapshot)), root=root)
    verify_query_receipt(receipt, evidence, expected_contract=args.contract)
    _print_json({
        "command": "query-verify",
        "verified": True,
        "contract": receipt["contract"],
        "release_id": receipt.get("release_id"),
    })
    return 0


def _ledger_command(args: argparse.Namespace) -> int:
    if not args.latest:
        raise InputError("CLI_ARGUMENT", "ledger 当前必须配合 --latest 使用")
    config = load_config(args.config)
    ledger_path = Path(args.db) if args.db else config.ledger_path
    ledger = Ledger(ledger_path, config.root)
    if not ledger_path.exists():
        raise InputError("LEDGER_NOT_FOUND", f"台账不存在: {ledger_path}")
    _print_json(ledger.export_latest())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command in {"demo", "run"}:
            return _run_command(args)
        if args.command == "serve":
            return _serve_command(args)
        if args.command == "ledger":
            return _ledger_command(args)
        if args.command == "query-release":
            return _query_release_command(args)
        if args.command == "query-verify":
            return _query_verify_command(args)
        raise InputError("CLI_ARGUMENT", f"未知命令: {args.command}")
    except InputError as exc:
        sys.stderr.write(stable_json_text({"error": exc.code, "message": exc.message, "details": exc.details}))
        return 3
    except SelregressError as exc:
        sys.stderr.write(stable_json_text({"error": exc.code, "message": exc.message}))
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
