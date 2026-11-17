import sqlite3
from contextlib import closing
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import storage
from app.config import Settings


def resolve_all(client: TestClient, seed_invoices: list[dict[str, Any]]) -> None:
    client.post("/invoices/import", json=seed_invoices)
    for item in seed_invoices:
        client.post(f"/invoices/{item['document_id']}/resolve")


def analytics_rows(settings: Settings, table: str) -> list[sqlite3.Row]:
    with closing(storage.connect(settings.analytics_db_path)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY document_id, resolution_id").fetchall()


def test_export_copies_each_resolution_once(
    client: TestClient, settings: Settings, seed_invoices: list[dict[str, Any]]
) -> None:
    resolve_all(client, seed_invoices)

    first = client.post("/exports/run").json()
    second = client.post("/exports/run").json()

    assert first["exported"] == 15
    assert second["exported"] == 0
    assert len(analytics_rows(settings, "resolved_invoices")) == 15
    exported_at = client.get("/invoices/doc_new_010").json()["exported_at"]
    assert exported_at == first["exported_at"]


def test_the_flat_row_has_every_column(
    client: TestClient, settings: Settings, seed_by_id: dict[str, dict[str, Any]]
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_011"])
    resolution = client.post("/invoices/doc_new_011/resolve").json()["resolution"]
    export = client.post("/exports/run").json()

    [row] = analytics_rows(settings, "resolved_invoices")

    assert dict(row) == {
        "document_id": "doc_new_011",
        "resolution_id": resolution["id"],
        "invoice_number": "AC-00192",
        "invoice_date": "2026-03-29",
        "supplier_name_raw": "Acme Consulting Europe",
        "supplier_tax_id": "FR998877665",
        "description": "Transformation workshop facilitation",
        "country": "FR",
        "currency": "EUR",
        "base_amount": 1800.0,
        "tax_amount": 0.0,
        "total_amount": 1800.0,
        "canonical_supplier": "ACME CONSULTING EUROPE",
        "is_new_supplier": 1,
        "predicted_spend_category": "other",
        "predicted_business_unit": "general_admin",
        "review_decision": "needs_review",
        "confidence": 0.4,
        "decision_explanation": resolution["result"]["decision_explanation"],
        "applied_rules": "RR4",
        "quality_flags": "",
        "tool_calls": 2,
        "provider": "fake",
        "model": "fake-scripted-v1",
        "resolved_at": resolution["created_at"],
        "exported_at": export["exported_at"],
    }
    # The final explanation, with the rules, not the model's own one.
    assert "Review rules applied: RR4" in row["decision_explanation"]


def test_reresolved_invoice_is_exported_again(
    client: TestClient, settings: Settings, seed_invoices: list[dict[str, Any]]
) -> None:
    resolve_all(client, seed_invoices)
    client.post("/exports/run")

    reresolved = client.post("/invoices/doc_new_001/resolve").json()
    export = client.post("/exports/run").json()

    assert reresolved["exported_at"] is None
    assert export["document_ids"] == ["doc_new_001"]
    assert len(analytics_rows(settings, "resolved_invoices")) == 16
    latest = analytics_rows(settings, "latest_resolved_invoices")
    assert len(latest) == 15
    orange = next(row for row in latest if row["document_id"] == "doc_new_001")
    assert orange["resolution_id"] == reresolved["resolution"]["id"]


def test_invoices_that_are_not_resolved_are_not_exported(
    client: TestClient,
    settings: Settings,
    seed_by_id: dict[str, dict[str, Any]],
    broken_llm: Any,
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_002"])  # stays "normalized"
    client.post("/invoices", json=seed_by_id["doc_new_003"])
    client.app.state.llm = broken_llm  # type: ignore[attr-defined]
    failed = client.post("/invoices/doc_new_003/resolve").json()

    export = client.post("/exports/run").json()

    assert failed["status"] == "failed"
    assert export["exported"] == 0
    assert analytics_rows(settings, "resolved_invoices") == []


def test_after_a_failed_reresolution_analytics_keeps_the_last_export(
    client: TestClient,
    settings: Settings,
    seed_by_id: dict[str, dict[str, Any]],
    broken_llm: Any,
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_002"])
    first = client.post("/invoices/doc_new_002/resolve").json()
    client.post("/exports/run")
    client.app.state.llm = broken_llm  # type: ignore[attr-defined]

    failed = client.post("/invoices/doc_new_002/resolve").json()
    export = client.post("/exports/run").json()

    assert (failed["status"], failed["exported_at"], export["exported"]) == ("failed", None, 0)
    [row] = analytics_rows(settings, "latest_resolved_invoices")
    assert row["resolution_id"] == first["resolution"]["id"]


def test_a_failed_analytics_write_leaves_the_invoices_pending(
    client: TestClient,
    seed_by_id: dict[str, dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_002"])
    client.post("/invoices/doc_new_002/resolve")

    def locked(*args: Any) -> None:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(storage, "insert_analytics_rows", locked)
    with pytest.raises(sqlite3.OperationalError):
        client.post("/exports/run")

    # The analytics write comes first, so a failure there marks nothing as exported.
    assert client.get("/invoices/doc_new_002").json()["exported_at"] is None


def test_a_run_that_stops_before_marking_is_repeated_without_duplicates(
    client: TestClient,
    settings: Settings,
    seed_invoices: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolve_all(client, seed_invoices)

    def stopped(*args: Any) -> None:
        raise RuntimeError("process stopped after the analytics commit")

    with monkeypatch.context() as patch:
        patch.setattr(storage, "mark_exported", stopped)
        with pytest.raises(RuntimeError):
            client.post("/exports/run")
    retry = client.post("/exports/run").json()

    # Nothing was marked, so the retry exports all 15 again; INSERT OR REPLACE keeps 15 rows.
    assert retry["exported"] == 15
    assert len(analytics_rows(settings, "resolved_invoices")) == 15
