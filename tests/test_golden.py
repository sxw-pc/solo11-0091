import json

from selregress.util import PROJECT_ROOT, sha256_file

GOLDEN = json.loads((PROJECT_ROOT / "tests/golden/report_sha256.json").read_text(encoding="utf-8"))
EXPECTED = json.loads(
    (PROJECT_ROOT / "fixtures/expected/demo_expected.json").read_text(encoding="utf-8")
)


def report_file(name):
    return PROJECT_ROOT / GOLDEN["workspace_report_dir"] / name


def test_golden_json_report(shared_execution):
    assert shared_execution.result.run_id == GOLDEN["run_id"]
    payload = json.loads(report_file("report.json").read_text(encoding="utf-8"))
    assert payload["summary"] == EXPECTED["summary"]
    assert payload["run"]["status"] == EXPECTED["status"]
    assert sha256_file(report_file("report.json")) == GOLDEN["json_sha256"]


def test_golden_markdown_report(shared_execution):
    assert shared_execution.result.run_id == GOLDEN["run_id"]
    text = report_file("report.md").read_text(encoding="utf-8")
    assert "择约回归台运行报告" in text
    assert sha256_file(report_file("report.md")) == GOLDEN["md_sha256"]


def test_golden_html_report(shared_execution):
    html = report_file("report.html").read_text(encoding="utf-8")
    assert shared_execution.result.status == EXPECTED["status"]
    assert "<script" not in html.lower() and "<link" not in html.lower()
    assert sha256_file(report_file("report.html")) == GOLDEN["html_sha256"]
