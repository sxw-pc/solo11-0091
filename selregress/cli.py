"""命令行入口：demo / run / serve / ledger。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .config import load_config
from .ledger import Ledger
from .runner import execute
from .util import InputError, SelregressError, stable_json_text

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
    table = sub.add_parser("table-evidence", help="核对本地表格合同输入，不生成记录")
    table.add_argument("--manifest", default="fixtures/table_contract/manifest.json")
    asset = sub.add_parser(
        "asset-evidence",
        help="离线清点本地授权照片：派生证据与按合同的许可单元裁决",
    )
    asset.add_argument("--manifest", default="fixtures/asset_contract/manifest.json")
    asset.add_argument(
        "--format", choices=("json", "markdown"), default="json",
        help="json 输出完整可复核证据，markdown 输出人类可读裁决摘要",
    )
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
        if args.command == "table-evidence":
            from .table_evidence import inspect_table_contract
            _print_json(inspect_table_contract(args.manifest))
            return 0
        if args.command == "asset-evidence":
            from .asset_evidence import inspect_asset_contract
            from .reporting import render_asset_contract_markdown
            evidence = inspect_asset_contract(args.manifest)
            if args.format == "markdown":
                sys.stdout.write(render_asset_contract_markdown(evidence))
                sys.stdout.flush()
            else:
                _print_json(evidence)
            return 0
        raise InputError("CLI_ARGUMENT", f"未知命令: {args.command}")
    except InputError as exc:
        sys.stderr.write(stable_json_text({"error": exc.code, "message": exc.message, "details": exc.details}))
        return 3
    except SelregressError as exc:
        sys.stderr.write(stable_json_text({"error": exc.code, "message": exc.message}))
        return 3
    except ValueError as exc:
        # asset_evidence.AssetEvidenceError：输入不可信/证据不足，拒绝自动少计
        sys.stderr.write(stable_json_text({"error": "ASSET_EVIDENCE_REJECTED", "message": str(exc)}))
        return 3
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
