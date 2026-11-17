import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

SeedById = dict[str, dict[str, Any]]


def test_create_invoice_stores_raw_and_normalized_payloads(
    client: TestClient, seed_by_id: SeedById
) -> None:
    payload = seed_by_id["doc_new_001"]

    response = client.post("/invoices", json=payload)

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "normalized"
    assert body["raw_payload"] == payload
    assert body["normalized"]["invoice_date"] == "2026-03-02"
    assert body["normalized"]["total_amount"] == "99.99"
    assert body["quality_flags"] == ["missing:supplier_tax_id"]
    assert body["exported_at"] is None
    assert client.get("/invoices/doc_new_001").json() == body


def test_create_duplicate_invoice_returns_409(client: TestClient, seed_by_id: SeedById) -> None:
    payload = seed_by_id["doc_new_002"]
    client.post("/invoices", json=payload)

    response = client.post("/invoices", json=payload)

    assert response.status_code == 409


@pytest.mark.parametrize(
    "payload",
    [
        {"supplier_name": "Orange"},  # no document_id
        {"document_id": "inv/2026/001"},  # a slash could never be used in the URL
        {"document_id": "doc_x", "field_confidence": {"supplier_name": "NaN"}},
        {"document_id": "doc_x", "field_confidence": {"supplier_name": 35}},  # a percentage
        {"document_id": "doc_x", "total_amount": True},
    ],
)
def test_create_invoice_with_invalid_payload_returns_422(
    client: TestClient, payload: dict[str, Any]
) -> None:
    response = client.post("/invoices", json=payload)

    assert response.status_code == 422
    assert client.get("/invoices/doc_x").status_code == 404


def test_numbers_are_accepted_where_text_is_expected(client: TestClient) -> None:
    response = client.post("/invoices", json={"document_id": "doc_num", "invoice_number": 12345})

    assert response.status_code == 201
    assert response.json()["normalized"]["invoice_number"] == "12345"


def test_get_unknown_invoice_returns_404(client: TestClient) -> None:
    assert client.get("/invoices/doc_missing").status_code == 404


def test_import_seed_twice_reports_duplicates(
    client: TestClient, seed_invoices: list[dict[str, Any]]
) -> None:
    first = client.post("/invoices/import", json=seed_invoices)
    second = client.post("/invoices/import", json=seed_invoices)

    assert first.status_code == 200
    assert len(first.json()["created"]) == 15
    assert second.json()["created"] == []
    assert len(second.json()["duplicates"]) == 15


def test_import_reports_invalid_items_and_keeps_the_rest(
    client: TestClient, seed_by_id: SeedById
) -> None:
    bad_confidence = {"document_id": "doc_x", "field_confidence": {"description": "NaN"}}
    items = [seed_by_id["doc_new_003"], {"supplier_name": "No id"}, "text", bad_confidence]

    response = client.post("/invoices/import", json=items)

    body = response.json()
    assert body["created"] == ["doc_new_003"]
    assert [item["index"] for item in body["invalid"]] == [1, 2, 3]
    assert "document_id" in body["invalid"][0]["error"]
    assert body["invalid"][2]["document_id"] == "doc_x"
    assert client.get("/invoices/doc_x").status_code == 404


# Expected outcome of every seed invoice with the fake LLM:
# (canonical supplier, category, business unit, review decision, review rules applied)
EXPECTED_RESOLUTIONS = {
    "doc_new_001": ("ORANGE ESPAGNE SA", "telecom", "it", "auto_approve", []),
    "doc_new_002": ("GOOGLE CLOUD EMEA LTD", "software", "it", "auto_approve", []),
    "doc_new_003": ("GOOGLE ADS FRANCE", "professional_services", "marketing", "auto_approve", []),
    "doc_new_004": ("STAPLES IBERIA SA", "office_supplies", "general_admin", "auto_approve", []),
    "doc_new_005": ("ENDESA ENERGIA SAU", "utilities", "operations", "auto_approve", []),
    "doc_new_006": ("IBERIA VIAJES CORPORATE SL", "travel", "sales", "auto_approve", []),
    "doc_new_007": (
        "DELOITTE ADVISORY SL",
        "professional_services",
        "general_admin",
        "auto_approve",
        [],
    ),
    "doc_new_008": ("MICROSOFT IRELAND OPERATIONS LTD", "software", "it", "auto_approve", []),
    "doc_new_009": ("UBER BV", "travel", "sales", "auto_approve", []),
    "doc_new_010": ("AMAZON", "other", "general_admin", "needs_review", ["RR1", "RR2", "RR4"]),
    "doc_new_011": ("ACME CONSULTING EUROPE", "other", "general_admin", "needs_review", ["RR4"]),
    "doc_new_012": ("AWS EMEA SARL", "software", "it", "auto_approve", []),
    "doc_new_013": ("ORANGE ESPAGNE SA", "telecom", "it", "auto_approve", []),
    "doc_new_014": ("GOOGLE ADS FRANCE", "professional_services", "marketing", "auto_approve", []),
    "doc_new_015": ("WEWORK SPAIN", "other", "general_admin", "needs_review", ["RR4"]),
}
NEW_SUPPLIERS = {"doc_new_010", "doc_new_011", "doc_new_015"}


def test_every_seed_invoice_resolves_as_expected(
    client: TestClient, seed_invoices: list[dict[str, Any]]
) -> None:
    client.post("/invoices/import", json=seed_invoices)

    for document_id, expected in EXPECTED_RESOLUTIONS.items():
        body = client.post(f"/invoices/{document_id}/resolve").json()

        resolution = body["resolution"]
        result = resolution["result"]
        found = (
            result["canonical_supplier"],
            result["predicted_spend_category"],
            result["predicted_business_unit"],
            result["review_decision"],
            [rule["rule_id"] for rule in resolution["applied_rules"]],
        )
        assert found == expected, document_id
        assert body["status"] == "resolved"
        assert result["is_new_supplier"] == (document_id in NEW_SUPPLIERS)
        assert [step["tool"] for step in resolution["trace"]] == [
            "match_canonical_supplier",
            "search_history",
        ]


def test_resolution_keeps_the_model_answer_and_the_trace(
    client: TestClient, seed_by_id: SeedById
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_010"])

    body = client.post("/invoices/doc_new_010/resolve").json()

    resolution = body["resolution"]
    assert resolution["model_answer"]["confidence"] == 0.4
    assert resolution["result"]["confidence"] == 0.4
    # The rules are added to the final explanation, never to the model's own answer.
    assert "Review rules applied: RR1:" in resolution["result"]["decision_explanation"]
    assert "Review rules applied" not in resolution["model_answer"]["decision_explanation"]
    assert resolution["trace"][0]["summary"] == "best AWS EMEA SARL (fuzzy 0.80), 1 candidate(s)"
    assert (resolution["provider"], resolution["model_turns"]) == ("fake", 3)
    assert client.get("/invoices/doc_new_010").json() == body


def test_invoice_without_supplier_data_goes_to_review(client: TestClient) -> None:
    client.post("/invoices", json={"document_id": "doc_blank", "description": "Services"})

    body = client.post("/invoices/doc_blank/resolve").json()

    resolution = body["resolution"]
    assert body["status"] == "resolved"
    assert [step["is_error"] for step in resolution["trace"]] == [True, True]
    assert resolution["result"]["review_decision"] == "needs_review"
    assert [rule["rule_id"] for rule in resolution["applied_rules"]] == ["RR1", "RR4"]


def test_each_resolve_call_creates_a_new_resolution(
    client: TestClient, seed_by_id: SeedById
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_002"])

    first = client.post("/invoices/doc_new_002/resolve").json()["resolution"]["id"]
    second = client.post("/invoices/doc_new_002/resolve").json()["resolution"]["id"]

    assert second > first


def test_failed_run_is_stored_without_business_fields(
    client: TestClient, seed_by_id: SeedById, broken_llm: Any
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_002"])
    client.app.state.llm = broken_llm  # type: ignore[attr-defined]

    response = client.post("/invoices/doc_new_002/resolve")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "failed"
    assert body["resolution"]["result"] is None
    assert body["resolution"]["error"] == "LLM call failed: APIConnectionError: Connection error."


def test_resolve_unknown_invoice_returns_404(client: TestClient) -> None:
    assert client.post("/invoices/doc_missing/resolve").status_code == 404


def test_health_names_the_llm_provider(client: TestClient) -> None:
    assert client.get("/health").json() == {
        "status": "ok",
        "llm_provider": "fake",
        "llm_model": "fake-scripted-v1",
    }


def test_resolution_logs_one_line_per_tool_call(
    client: TestClient, seed_by_id: SeedById, caplog: pytest.LogCaptureFixture
) -> None:
    client.post("/invoices", json=seed_by_id["doc_new_001"])
    caplog.set_level(logging.INFO)

    client.post("/invoices/doc_new_001/resolve")

    lines = [record.getMessage() for record in caplog.records]
    tool_lines = [line for line in lines if line.startswith("doc_new_001 | turn ")]
    assert tool_lines[0].startswith('doc_new_001 | turn 1 | tool match_canonical_supplier {"')
    assert "-> best ORANGE ESPAGNE SA (alias 0.95), 1 candidate(s)" in tool_lines[0]
    assert tool_lines[1].startswith("doc_new_001 | turn 2 | tool search_history")
    assert "doc_new_001 | review rules: none -> auto_approve, confidence 0.85" in lines
    assert not [record for record in caplog.records if record.name == "httpx2"]
