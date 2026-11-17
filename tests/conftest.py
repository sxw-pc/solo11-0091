from __future__ import annotations

import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from selregress.config import AppConfig, load_config
from selregress.runner import Execution, execute
from selregress.server import create_app
from selregress.util import PROJECT_ROOT

TMP_ROOT = Path(__file__).resolve().parent / "_tmp"


def _clean(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture(scope="session")
def shared_config() -> AppConfig:
    return load_config(PROJECT_ROOT / "configs/demo.yaml", PROJECT_ROOT)


@pytest.fixture(scope="session")
def shared_execution(shared_config: AppConfig) -> Execution:
    _clean(TMP_ROOT / "shared")
    execution = execute(
        shared_config,
        ledger_path=TMP_ROOT / "shared" / "ledger.db",
        runs_dir=TMP_ROOT / "shared" / "runs",
    )
    yield execution
    _clean(TMP_ROOT / "shared")


@pytest.fixture()
def workspace(request: pytest.FixtureRequest) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", request.node.nodeid)[-90:]
    path = TMP_ROOT / "cases" / name
    _clean(path)
    path.mkdir(parents=True, exist_ok=True)
    yield path
    _clean(path)


@pytest.fixture()
def app(shared_config: AppConfig, shared_execution: Execution):
    config = replace(
        shared_config,
        ledger_path=TMP_ROOT / "shared" / "ledger.db",
        runs_dir=TMP_ROOT / "shared" / "runs",
    )
    application = create_app(config)
    application.config.update(TESTING=True)
    return application
