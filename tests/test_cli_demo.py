import json
import subprocess
import sys

from selregress.util import PROJECT_ROOT

TMP_ROOT = PROJECT_ROOT / "tests/_tmp"


def cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "selregress", *args],
        cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=90,
    )


def demo_args(workspace, *extra):
    return (
        "demo", "--config", "configs/demo.yaml",
        "--fixed-time", "2024-03-01T00:00:00Z",
        "--db", str(workspace / "ledger.db"),
        "--out-dir", str(workspace / "runs"),
        *extra,
    )


def test_demo_cli_runs_end_to_end(workspace):
    result = cli(*demo_args(workspace))
    assert result.returncode == 2, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run"]["status"] == "warn"
    assert payload["run"]["exit_code"] == 2
    assert payload["run"]["summary"]["field_result_count"] == 40
    for key in ("json", "markdown", "html"):
        path = PROJECT_ROOT / payload["run"]["reports"][key]
        assert path.is_file()
    assert (workspace / "ledger.db").is_file()


def test_ledger_latest_cli(workspace):
    assert cli(*demo_args(workspace)).returncode == 2
    result = cli(
        "ledger", "--latest", "--config", "configs/demo.yaml",
        "--db", str(workspace / "ledger.db"),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["run"]["status"] == "warn"
    assert payload["run"]["finding_count"] == 11


def test_fail_on_warn_changes_exit_code(workspace):
    result = cli(*demo_args(workspace, "--fail-on-warn"))
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["run"]["status"] == "warn"
    assert payload["run"]["exit_code"] == 1


def test_invalid_config_returns_three(workspace):
    result = cli(
        "run", "--config", "configs/does-not-exist.yaml",
        "--db", str(workspace / "ledger.db"),
    )
    assert result.returncode == 3
    payload = json.loads(result.stderr)
    assert payload["error"] == "CONFIG_NOT_FOUND"


def test_run_command_and_ledger_history(workspace):
    first = cli("run", *demo_args(workspace)[1:])
    second = cli("run", *demo_args(workspace)[1:])
    assert first.returncode == 2
    assert second.returncode == 2
    first_id = json.loads(first.stdout)["run"]["run_id"]
    second_id = json.loads(second.stdout)["run"]["run_id"]
    assert first_id != second_id
    assert second_id.endswith("-2")
    result = cli(
        "ledger", "--latest", "--config", "configs/demo.yaml",
        "--db", str(workspace / "ledger.db"),
    )
    assert json.loads(result.stdout)["run"]["run_id"] == second_id
