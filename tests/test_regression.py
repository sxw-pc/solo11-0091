from selregress.models import BaselineDiff, Finding, exit_code_for
from selregress.regression import decide_run_status, summarize


def finding(severity):
    return Finding(
        code="no_match", severity=severity, message="x", recipe_name="r",
        site="site-a.invalid", page_type="detail", version="2024-01",
        snapshot_path="fixtures/x.html", field_name="f",
    )


def test_status_and_exit_codes():
    assert decide_run_status([], []) == "pass"
    assert decide_run_status([finding("warn")], []) == "warn"
    assert decide_run_status([finding("fail")], []) == "fail"
    assert exit_code_for("pass") == 0
    assert exit_code_for("warn") == 2
    assert exit_code_for("warn", fail_on_warn=True) == 1
    assert exit_code_for("fail") == 1
    assert exit_code_for("fail", fail_on_warn=True) == 1


def test_summary_pass():
    status, summary = summarize([], [], [], recipe_count=1, snapshot_count=1)
    assert status == "pass"
    assert summary["finding_count"] == 0
    assert summary["average_stability"] == 100
    assert summary["hit_rate_percent"] == 100


def test_summary_fail_wins_over_warn():
    status, summary = summarize(
        [], [finding("warn"), finding("fail")], [], recipe_count=1, snapshot_count=1
    )
    assert status == "fail"
    assert summary["fail_finding_count"] == 1
    assert summary["warn_finding_count"] == 1


def test_baseline_diff_affects_run_status():
    diff = BaselineDiff(
        change_type="value_changed", severity="warn", recipe_name="r",
        site="site-a.invalid", page_type="detail", field_name="f",
        old_value="a", new_value="b", message="changed",
    )
    status, summary = summarize([], [], [diff], recipe_count=1, snapshot_count=1)
    assert status == "warn"
    assert summary["baseline_warn_count"] == 1


def test_shared_execution_is_warn_with_expected_counts(shared_execution):
    result = shared_execution.result
    assert result.status == "warn"
    assert result.exit_code == 2
    assert result.summary["recipe_count"] == 3
    assert result.summary["snapshot_count"] == 6
    assert result.summary["field_result_count"] == 40
    assert result.summary["fail_finding_count"] == 0
    assert result.summary["hit_rate_percent"] == 92
    assert result.summary["version_drift_field_count"] == 3
