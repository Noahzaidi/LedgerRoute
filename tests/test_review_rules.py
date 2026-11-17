import sqlite3
from collections.abc import Callable
from typing import Any

import pytest

from app import storage
from app.normalization import normalize_invoice
from app.reference import ReferenceData
from app.review_rules import apply_review_rules, gather_evidence
from app.schemas import AgentAnswer, InvoiceIn, Resolution

ORANGE_ANSWER = {
    "canonical_supplier": "ORANGE ESPAGNE SA",
    "predicted_spend_category": "telecom",
    "predicted_business_unit": "it",
    "review_decision": "auto_approve",
    "confidence": 0.9,
    "decision_explanation": "Matched by tax ID; history is telecom for it.",
}
NEW_SUPPLIER_ANSWER = {
    "canonical_supplier": "AMAZON",
    "predicted_spend_category": "other",
    "predicted_business_unit": "general_admin",
    "review_decision": "needs_review",
    "confidence": 0.4,
    "decision_explanation": "No known supplier matches.",
}

Resolve = Callable[[str | dict[str, Any], dict[str, Any]], tuple[Resolution, list[str]]]


@pytest.fixture
def resolve(
    seed_by_id: dict[str, dict[str, Any]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> Resolve:
    """resolve(seed document_id or payload, model answer) -> (resolution, rule ids)."""

    def run(invoice: str | dict[str, Any], answer: dict[str, Any]) -> tuple[Resolution, list[str]]:
        payload = seed_by_id[invoice] if isinstance(invoice, str) else invoice
        normalized, flags = normalize_invoice(InvoiceIn.model_validate(payload))
        model_answer = AgentAnswer.model_validate(answer)
        evidence = gather_evidence(normalized, model_answer, history_db, reference)
        resolution, rules = apply_review_rules(model_answer, flags, evidence, reference)
        return resolution, [rule.rule_id for rule in rules]

    return run


def test_proven_supplier_with_consistent_history_keeps_the_answer(resolve: Resolve) -> None:
    resolution, rules = resolve("doc_new_013", ORANGE_ANSWER)

    assert rules == []
    assert resolution.review_decision == "auto_approve"
    assert resolution.confidence == 0.9
    assert resolution.decision_explanation == ORANGE_ANSWER["decision_explanation"]
    assert resolution.is_new_supplier is False


def test_amazon_triggers_rr1_rr2_and_rr4(resolve: Resolve) -> None:
    resolution, rules = resolve("doc_new_010", NEW_SUPPLIER_ANSWER)

    assert rules == ["RR1", "RR2", "RR4"]
    assert resolution.review_decision == "needs_review"
    assert resolution.is_new_supplier is True
    assert resolution.decision_explanation.startswith(
        "No known supplier matches. Review rules applied: RR1: No tax ID and no proven supplier."
    )


def test_rules_override_a_model_that_maps_amazon_to_aws(resolve: Resolve) -> None:
    aws_answer = {
        **NEW_SUPPLIER_ANSWER,
        "canonical_supplier": "AWS EMEA SARL",
        "predicted_spend_category": "software",
        "predicted_business_unit": "it",
        "review_decision": "auto_approve",
        "confidence": 0.9,
    }

    resolution, rules = resolve("doc_new_010", aws_answer)

    assert rules == ["RR1", "RR2", "RR4"]
    assert resolution.review_decision == "needs_review"
    assert resolution.confidence == 0.6
    assert resolution.is_new_supplier is False


def test_new_supplier_with_tax_id_still_needs_review(resolve: Resolve) -> None:
    acme_answer = {**NEW_SUPPLIER_ANSWER, "canonical_supplier": "ACME CONSULTING EUROPE"}

    _, rules = resolve("doc_new_011", acme_answer)

    assert rules == ["RR4"]


def test_tax_id_and_name_of_different_suppliers_trigger_rr3(resolve: Resolve) -> None:
    invoice = {
        "document_id": "doc_conflict",
        "supplier_name": "Google Ads",  # an alias of GOOGLE ADS FRANCE
        "supplier_tax_id": "IE6388047V",  # the tax ID of GOOGLE CLOUD EMEA LTD
    }
    cloud_answer = {
        **ORANGE_ANSWER,
        "canonical_supplier": "GOOGLE CLOUD EMEA LTD",
        "predicted_spend_category": "software",
    }

    resolution, rules = resolve(invoice, cloud_answer)

    assert rules == ["RR3"]
    assert resolution.review_decision == "needs_review"
    assert "point to different suppliers" in resolution.decision_explanation


def test_alias_contradicted_by_another_tax_id_triggers_rr4(resolve: Resolve) -> None:
    invoice = {"document_id": "doc_x", "supplier_name": "Staples", "supplier_tax_id": "ESB12345678"}
    staples_answer = {
        **ORANGE_ANSWER,
        "canonical_supplier": "STAPLES IBERIA SA",
        "predicted_spend_category": "office_supplies",
        "predicted_business_unit": "general_admin",
    }

    resolution, rules = resolve(invoice, staples_answer)

    assert rules == ["RR4"]
    assert "the invoice's tax ID is not that supplier's" in resolution.decision_explanation


def test_category_that_contradicts_history_triggers_rr3(resolve: Resolve) -> None:
    resolution, rules = resolve(
        "doc_new_013", {**ORANGE_ANSWER, "predicted_spend_category": "software"}
    )

    assert rules == ["RR3"]
    assert "History files ORANGE ESPAGNE SA under telecom" in resolution.decision_explanation


def test_supplier_that_contradicts_the_tax_id_triggers_rr3(resolve: Resolve) -> None:
    _, rules = resolve("doc_new_013", {**ORANGE_ANSWER, "canonical_supplier": "UBER BV"})

    assert rules == ["RR3"]


def test_split_history_triggers_rr3(
    resolve: Resolve, history_db: sqlite3.Connection, history_rows: list[dict[str, Any]]
) -> None:
    orange_row = next(row for row in history_rows if row["history_id"] == "hist_001")
    storage.seed_history(
        history_db,
        [{**orange_row, "history_id": "hist_extra", "predicted_spend_category": "software"}],
    )

    resolution, rules = resolve("doc_new_013", ORANGE_ANSWER)

    assert rules == ["RR3"]
    assert "several categories" in resolution.decision_explanation


def test_business_unit_that_contradicts_history_triggers_rr3(resolve: Resolve) -> None:
    resolution, rules = resolve(
        "doc_new_013", {**ORANGE_ANSWER, "predicted_business_unit": "sales"}
    )

    assert rules == ["RR3"]
    assert "History assigns ORANGE ESPAGNE SA to it" in resolution.decision_explanation


def test_low_model_confidence_cannot_auto_approve(resolve: Resolve) -> None:
    resolution, rules = resolve("doc_new_013", {**ORANGE_ANSWER, "confidence": 0.65})

    assert rules == ["LOW_CONFIDENCE"]
    assert resolution.review_decision == "needs_review"
    assert resolution.confidence == 0.6


def test_confidence_at_the_threshold_can_auto_approve(resolve: Resolve) -> None:
    resolution, rules = resolve("doc_new_013", {**ORANGE_ANSWER, "confidence": 0.70})

    assert rules == []
    assert resolution.review_decision == "auto_approve"


def test_known_supplier_written_differently_gets_the_exact_name(resolve: Resolve) -> None:
    resolution, rules = resolve(
        "doc_new_013", {**ORANGE_ANSWER, "canonical_supplier": "Orange Espagne SA"}
    )

    assert rules == []
    assert resolution.canonical_supplier == "ORANGE ESPAGNE SA"
    assert resolution.is_new_supplier is False
