import json
import re
from pathlib import Path

from selregress.reporting import build_report, render_markdown, stable_json_text
from selregress.util import PROJECT_ROOT, sha256_text


def report_path(execution, name):
    return PROJECT_ROOT / execution.result.report_dir / name


def test_report_json_structure_and_stability(shared_execution):
    report = build_report(shared_execution.result)
    assert report["schema_version"] == 1
    assert report["run"]["fixed_time"] is True
    assert report["summary"]["field_result_count"] == 40
    first = stable_json_text(report)
    second = stable_json_text(build_report(shared_execution.result))
    assert first == second
    assert first.endswith("\n")
    assert sha256_text(first) == shared_execution.result.report_hashes["json"]


def test_report_sorting_rules(shared_execution):
    report = build_report(shared_execution.result)
    field_keys = [
        (item["recipe_name"], item["site"], item["page_type"], item["version"],
         item["snapshot_path"], item["field_name"])
        for item in report["fields"]
    ]
    assert field_keys == sorted(field_keys)
    severity_order = {"fail": 0, "warn": 1, "info": 2, "pass": 3}
    finding_ranks = [severity_order[item["severity"]] for item in report["findings"]]
    assert finding_ranks == sorted(finding_ranks)
    diff_keys = [
        (item["recipe_name"], item["site"], item["page_type"], item["field_name"],
         item["change_type"], item["snapshot_path"])
        for item in report["baseline_diffs"]
    ]
    assert diff_keys == sorted(diff_keys)


def test_markdown_sections_and_escaped_values(shared_execution):
    report = build_report(shared_execution.result)
    markdown = render_markdown(report)
    for heading in [
        "## 总览", "## 字段矩阵", "## 失败字段", "## 发现项",
        "## 跨版本漂移", "## 候选选择器建议", "## 基线差异",
    ]:
        assert heading in markdown
    assert "候选选择器" in markdown
    assert "示例描述" in markdown
    assert markdown.endswith("\n")


def test_html_report_is_self_contained_and_escaped(shared_execution):
    html = report_path(shared_execution, "report.html").read_text(encoding="utf-8")
    assert "<style>" in html
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    assert "http://" not in html.lower()
    assert "https://" not in html.lower()
    assert "&lt;p&gt;示例描述" in html
    assert "<p>示例描述" not in html
    assert "候选选择器建议" in html


def test_markdown_and_html_hashes_match_ledger(shared_execution):
    for name, key in (("report.md", "md"), ("report.html", "html")):
        text = report_path(shared_execution, name).read_text(encoding="utf-8")
        assert sha256_text(text) == shared_execution.result.report_hashes[key]
    payload = json.loads(report_path(shared_execution, "report.json").read_text(encoding="utf-8"))
    assert payload["run"]["run_id"] == shared_execution.result.run_id
