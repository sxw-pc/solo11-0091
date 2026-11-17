"""SQLite 台账 DDL 与只读视图常量。"""

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS run (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        generated_at TEXT NOT NULL,
        fixed_time INTEGER NOT NULL,
        config_path TEXT NOT NULL,
        fail_on_warn INTEGER NOT NULL,
        recipe_count INTEGER NOT NULL,
        snapshot_count INTEGER NOT NULL,
        field_result_count INTEGER NOT NULL,
        finding_count INTEGER NOT NULL,
        baseline_diff_count INTEGER NOT NULL,
        report_dir TEXT NOT NULL,
        json_sha256 TEXT NOT NULL,
        md_sha256 TEXT NOT NULL,
        html_sha256 TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS recipe (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_pk INTEGER NOT NULL REFERENCES run(id),
        name TEXT NOT NULL,
        site TEXT NOT NULL,
        page_type TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        baseline TEXT,
        field_count INTEGER NOT NULL,
        UNIQUE(run_pk, name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS snapshot (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_pk INTEGER NOT NULL REFERENCES run(id),
        site TEXT NOT NULL,
        page_type TEXT NOT NULL,
        version TEXT NOT NULL,
        path TEXT NOT NULL,
        sha256 TEXT NOT NULL,
        size INTEGER NOT NULL,
        captured_at TEXT NOT NULL,
        warnings_json TEXT NOT NULL,
        UNIQUE(run_pk, path)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS field_result (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_pk INTEGER NOT NULL REFERENCES run(id),
        recipe_name TEXT NOT NULL,
        site TEXT NOT NULL,
        page_type TEXT NOT NULL,
        version TEXT NOT NULL,
        snapshot_path TEXT NOT NULL,
        snapshot_sha256 TEXT NOT NULL,
        field_name TEXT NOT NULL,
        field_type TEXT NOT NULL,
        required INTEGER NOT NULL,
        multi INTEGER NOT NULL,
        used_selector TEXT NOT NULL,
        raw_values_json TEXT NOT NULL,
        normalized_value_json TEXT NOT NULL,
        match_count INTEGER NOT NULL,
        conversion_status TEXT NOT NULL,
        flags_json TEXT NOT NULL,
        score INTEGER NOT NULL,
        status TEXT NOT NULL,
        error TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS finding (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_pk INTEGER NOT NULL REFERENCES run(id),
        code TEXT NOT NULL,
        severity TEXT NOT NULL,
        message TEXT NOT NULL,
        recipe_name TEXT NOT NULL,
        site TEXT NOT NULL,
        page_type TEXT NOT NULL,
        version TEXT NOT NULL,
        snapshot_path TEXT NOT NULL,
        field_name TEXT,
        evidence_json TEXT NOT NULL,
        suggestions_json TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS baseline_diff (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_pk INTEGER NOT NULL REFERENCES run(id),
        change_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        recipe_name TEXT NOT NULL,
        site TEXT NOT NULL,
        page_type TEXT NOT NULL,
        field_name TEXT NOT NULL,
        old_value_json TEXT NOT NULL,
        new_value_json TEXT NOT NULL,
        message TEXT NOT NULL,
        snapshot_path TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_field_run ON field_result(run_pk)",
    "CREATE INDEX IF NOT EXISTS idx_field_filter ON field_result(run_pk, site, page_type, field_name)",
    "CREATE INDEX IF NOT EXISTS idx_field_status ON field_result(run_pk, status)",
    "CREATE INDEX IF NOT EXISTS idx_finding_run ON finding(run_pk)",
    "CREATE INDEX IF NOT EXISTS idx_finding_severity ON finding(run_pk, severity)",
    "CREATE INDEX IF NOT EXISTS idx_diff_run ON baseline_diff(run_pk)",
    "CREATE INDEX IF NOT EXISTS idx_snapshot_run ON snapshot(run_pk)",
    "CREATE INDEX IF NOT EXISTS idx_run_status ON run(status, generated_at)",
]
