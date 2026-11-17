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
    notice = sub.add_parser("notice-intake", help="核对本地公告列表与详情材料")
    notice.add_argument("--manifest", required=True, help="本地公告材料清单")
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


def _notice_intake_command(args: argparse.Namespace) -> int:
    from .notice_intake import intake_notices

    result = intake_notices(args.manifest)
    _print_json(result)
    return 0 if result["status"] == "ready" else 2


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        if args.command in {"demo", "run"}:
            return _run_command(args)
        if args.command == "serve":
            return _serve_command(args)
        if args.command == "ledger":
            return _ledger_command(args)
        if args.command == "notice-intake":
            return _notice_intake_command(args)
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
