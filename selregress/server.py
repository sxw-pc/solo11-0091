"""只读 Flask 复核台。无写接口、无外部资源、仅绑定 127.0.0.1。"""

from __future__ import annotations

from flask import Flask, abort, request

from .config import AppConfig
from .ledger_query import LedgerQuery
from .util import InputError
from .views import register_routes


def create_app(config: AppConfig) -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(
        JSON_SORT_KEYS=True,
        SELREGRESS_CONFIG_NAME=config.name,
        SELREGRESS_CONFIG_PATH=config.config_relpath,
        SELREGRESS_DB_PATH=config.ledger_path.name,
    )
    ledger = LedgerQuery(config.ledger_path, config.root)
    app.extensions["selregress_ledger"] = ledger
    app.extensions["selregress_config"] = config

    @app.before_request
    def readonly_methods():  # type: ignore[no-untyped-def]
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            abort(405)

    @app.after_request
    def security_headers(response):  # type: ignore[no-untyped-def]
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'none'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.route("/healthz")
    def healthz():  # type: ignore[no-untyped-def]
        try:
            health = ledger.health()
        except InputError:
            return {
                "status": "degraded",
                "service": "selregress",
                "read_only": True,
                "database": config.ledger_path.name,
                "reason": "ledger unavailable",
            }, 503
        health.update({
            "service": "selregress",
            "read_only": True,
            "database": config.ledger_path.name,
            "config": config.config_relpath,
            "bind": "127.0.0.1",
        })
        return health

    register_routes(app, ledger)
    return app
