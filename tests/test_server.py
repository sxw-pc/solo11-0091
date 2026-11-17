from pathlib import Path

from selregress.ledger import Ledger
from selregress.util import PROJECT_ROOT

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def test_healthz_reports_local_read_only_state(app):
    response = app.test_client().get("/healthz")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["read_only"] is True
    assert payload["bind"] == "127.0.0.1"
    assert payload["database"] == "ledger.db"
    assert payload["tables"]["run"] >= 1


def test_index_lists_run_without_external_resources(app, shared_execution):
    response = app.test_client().get("/")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert shared_execution.result.run_id in html
    assert "/static/app.css" in html
    assert "http://" not in html
    assert "https://" not in html


def test_run_detail_contains_matrix_findings_and_diffs(app, shared_execution):
    response = app.test_client().get(f"/runs/{shared_execution.result.run_id}")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "字段结果矩阵" in html
    assert "featured_date" in html
    assert "ambiguous_selector" in html
    assert "值发生变化" in html


def test_snapshot_and_finding_detail(app, shared_execution):
    ledger = Ledger(TMP_ROOT / "shared" / "ledger.db", PROJECT_ROOT)
    run = ledger.get_run(shared_execution.result.run_id)
    snapshots = ledger.list_snapshots(run["id"])
    findings = ledger.query_findings(shared_execution.result.run_id)
    client = app.test_client()
    snapshot_response = client.get(f"/snapshots/{snapshots[0]['id']}")
    assert snapshot_response.status_code == 200
    assert snapshots[0]["path"] in snapshot_response.get_data(as_text=True)
    finding = next(item for item in findings if item["suggestions"])
    finding_response = client.get(f"/findings/{finding['id']}")
    assert finding_response.status_code == 200
    html = finding_response.get_data(as_text=True)
    assert "只读归因证据" in html
    assert "不得自动写回" in html


def test_missing_pages_return_404(app):
    client = app.test_client()
    assert client.get("/does-not-exist").status_code == 404
    assert client.get("/runs/not-a-run").status_code == 404
    assert client.get("/findings/999999").status_code == 404


def test_only_read_methods_are_allowed(app):
    client = app.test_client()
    assert client.post("/").status_code == 405
    assert client.put("/healthz").status_code == 405
    assert client.delete("/runs/x").status_code == 405


def test_security_headers_block_external_content(app):
    response = app.test_client().get("/")
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "connect-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_snapshot_values_are_html_escaped(app, shared_execution):
    ledger = Ledger(TMP_ROOT / "shared" / "ledger.db", PROJECT_ROOT)
    run = ledger.get_run(shared_execution.result.run_id)
    snapshots = ledger.list_snapshots(run["id"])
    target = next(item for item in snapshots if "detail-2024-02" in item["path"])
    html = app.test_client().get(f"/snapshots/{target['id']}").get_data(as_text=True)
    assert "&lt;p&gt;示例描述" in html
    assert "<p>示例描述" not in html
