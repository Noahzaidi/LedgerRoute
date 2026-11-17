"""Shared fixtures: every test gets fresh database files in its own temp folder."""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.config import PROJECT_ROOT, Settings
from app.llm import LLMError, ModelTurn
from app.main import create_app
from app.reference import ReferenceData, load_reference_data

SEED_DIR = PROJECT_ROOT / "data"


@pytest.fixture(autouse=True)
def no_settings_from_the_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must not depend on variables such as LLM_PROVIDER set in the developer's shell."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings isolated from the developer's `.env` and databases, with the fake LLM."""
    return Settings(
        _env_file=None,
        llm_provider="fake",
        operational_db_path=tmp_path / "operational.db",
        analytics_db_path=tmp_path / "analytics.db",
    )


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings)) as test_client:
        yield test_client


class BrokenLLM:
    """An LLM client whose every call fails, like an API outage."""

    provider = "broken"
    model = "broken"

    def complete(self, *args: Any) -> ModelTurn:
        raise LLMError("APIConnectionError: Connection error.")


@pytest.fixture
def broken_llm() -> BrokenLLM:
    return BrokenLLM()


@pytest.fixture(scope="session")
def seed_invoices() -> list[dict[str, Any]]:
    """The 15 post-OCR inputs shipped in data/."""
    text = (SEED_DIR / "new_post_ocr_inputs.json").read_text(encoding="utf-8")
    invoices: list[dict[str, Any]] = json.loads(text)
    return invoices


@pytest.fixture(scope="session")
def seed_by_id(seed_invoices: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The same inputs, looked up by document_id."""
    return {item["document_id"]: item for item in seed_invoices}


@pytest.fixture(scope="session")
def reference() -> ReferenceData:
    return load_reference_data(SEED_DIR / "reference_data.json")


@pytest.fixture(scope="session")
def history_rows() -> list[dict[str, Any]]:
    """The 51 resolved invoices shipped in data/."""
    text = (SEED_DIR / "historical_resolutions.json").read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = json.loads(text)
    return rows


@pytest.fixture
def history_db(tmp_path: Path, history_rows: list[dict[str, Any]]) -> Iterator[sqlite3.Connection]:
    """An operational database with the seed history loaded, as at service startup."""
    conn = storage.connect(tmp_path / "tools.db")
    storage.init_operational_db(conn)
    storage.seed_history(conn, history_rows)
    yield conn
    conn.close()
