"""The processing steps behind the API.

The API module only translates HTTP into these calls, so tests and the sample runner use
exactly the same steps as a real request.
"""

import logging
import sqlite3
from typing import Any

from pydantic import ValidationError

from app import storage
from app.agent import run_agent
from app.llm import LLMClient
from app.normalization import normalize_invoice
from app.reference import ReferenceData
from app.review_rules import apply_review_rules, gather_evidence
from app.schemas import (
    AppliedRule,
    ImportResult,
    InvalidItem,
    InvoiceIn,
    InvoiceRecord,
    Resolution,
    ResolutionRecord,
)

logger = logging.getLogger(__name__)


def ingest_invoice(conn: sqlite3.Connection, payload: InvoiceIn) -> bool:
    """Normalize and store one invoice. Returns False if its document_id already exists."""
    invoice, flags = normalize_invoice(payload)
    created = storage.insert_invoice(conn, payload.model_dump(exclude_unset=True), invoice, flags)
    conn.commit()
    if created:
        logger.info(
            "%s | ingested | quality flags: %s", invoice.document_id, ", ".join(flags) or "none"
        )
    else:
        logger.info("%s | ingest skipped | document_id already exists", invoice.document_id)
    return created


def import_invoices(conn: sqlite3.Connection, items: list[Any]) -> ImportResult:
    """Ingest a batch. An invalid item is reported and skipped; it does not stop the batch."""
    result = ImportResult(created=[], duplicates=[], invalid=[])
    for index, item in enumerate(items):
        try:
            payload = InvoiceIn.model_validate(item)
        except ValidationError as error:
            result.invalid.append(
                InvalidItem(
                    index=index,
                    document_id=document_id_of(item),
                    error=describe_validation_error(error),
                )
            )
            continue
        if ingest_invoice(conn, payload):
            result.created.append(payload.document_id)
        else:
            result.duplicates.append(payload.document_id)
    return result


def resolve_invoice(
    conn: sqlite3.Connection, document_id: str, llm: LLMClient, reference: ReferenceData
) -> InvoiceRecord | None:
    """Run the agent on a stored invoice, apply the review rules and store the outcome.

    Returns None if the invoice does not exist. Everything is written in one transaction at the
    end, so an interrupted run leaves the invoice exactly as it was.
    """
    invoice = storage.get_invoice(conn, document_id)
    if invoice is None:
        return None

    run = run_agent(invoice.normalized, invoice.quality_flags, llm, conn, reference)
    resolution: Resolution | None = None
    rules: list[AppliedRule] = []
    if run.answer is not None:
        evidence = gather_evidence(invoice.normalized, run.answer, conn, reference)
        resolution, rules = apply_review_rules(
            run.answer, invoice.quality_flags, evidence, reference
        )
        logger.info(
            "%s | review rules: %s -> %s, confidence %.2f",
            document_id,
            ", ".join(rule.rule_id for rule in rules) or "none",
            resolution.review_decision,
            resolution.confidence,
        )

    record = ResolutionRecord(
        status="failed" if resolution is None else "resolved",
        result=resolution,
        model_answer=run.answer,
        applied_rules=rules,
        trace=run.trace,
        error=run.error,
        provider=llm.provider,
        model=llm.model,
        model_turns=run.turns,
        created_at=storage.now_iso(),
    )
    storage.insert_resolution(conn, document_id, record)
    storage.mark_new_resolution(conn, document_id, record.status)
    conn.commit()
    logger.info(
        "%s | %s | %d model turns, %d tool calls",
        document_id,
        record.status,
        record.model_turns,
        len(record.trace),
    )
    return storage.get_invoice(conn, document_id)


def document_id_of(item: Any) -> str | None:
    """The document_id of an invalid item, if it has a readable one, for the error report."""
    value = item.get("document_id") if isinstance(item, dict) else None
    return value if isinstance(value, str) else None


def describe_validation_error(error: ValidationError) -> str:
    """'document_id: Field required; total_amount: ...' instead of Pydantic's long message."""
    parts = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"]) or "payload"
        parts.append(f"{location}: {detail['msg']}")
    return "; ".join(parts)
