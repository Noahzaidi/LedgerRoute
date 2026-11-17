"""Incremental export from the operational database to the analytical one.

The analytical table is flat: one row per exported resolution, with the invoice's key fields
next to the resolved business fields, ready for reporting or as a labelled dataset.
"""

import logging
import sqlite3
from decimal import Decimal
from typing import Any

from app import storage
from app.schemas import ExportResult, InvoiceRecord

logger = logging.getLogger(__name__)


def run_export(operational: sqlite3.Connection, analytics: sqlite3.Connection) -> ExportResult:
    """Copy every resolved invoice whose current resolution has not been exported yet."""
    pending = storage.invoices_pending_export(operational)
    exported_at = storage.now_iso()
    rows = [analytics_row(invoice, exported_at) for invoice in pending]

    storage.insert_analytics_rows(analytics, rows)
    analytics.commit()
    # Marked only after the analytical commit. If the process stops in between, the next run
    # exports the same rows again and INSERT OR REPLACE overwrites them: no duplicates.
    document_ids = [invoice.document_id for invoice in pending]
    storage.mark_exported(operational, document_ids, exported_at)
    operational.commit()

    logger.info("export | %d invoice(s) exported: %s", len(rows), ", ".join(document_ids) or "-")
    return ExportResult(exported=len(rows), document_ids=document_ids, exported_at=exported_at)


def analytics_row(invoice: InvoiceRecord, exported_at: str) -> dict[str, Any]:
    resolution = invoice.resolution
    if resolution is None or resolution.result is None or resolution.id is None:
        raise ValueError(f"{invoice.document_id} is marked resolved but has no resolution result")
    result = resolution.result
    normalized = invoice.normalized
    return {
        "document_id": invoice.document_id,
        "resolution_id": resolution.id,
        "invoice_number": normalized.invoice_number,
        "invoice_date": normalized.invoice_date.isoformat() if normalized.invoice_date else None,
        "supplier_name_raw": normalized.supplier_name,
        "supplier_tax_id": normalized.supplier_tax_id,
        "description": normalized.description,
        "country": normalized.country,
        "currency": normalized.currency,
        "base_amount": to_float(normalized.base_amount),
        "tax_amount": to_float(normalized.tax_amount),
        "total_amount": to_float(normalized.total_amount),
        "canonical_supplier": result.canonical_supplier,
        "is_new_supplier": result.is_new_supplier,
        "predicted_spend_category": result.predicted_spend_category,
        "predicted_business_unit": result.predicted_business_unit,
        "review_decision": result.review_decision,
        "confidence": result.confidence,
        "decision_explanation": result.decision_explanation,
        "applied_rules": ",".join(rule.rule_id for rule in resolution.applied_rules),
        "quality_flags": ",".join(invoice.quality_flags),
        "tool_calls": len(resolution.trace),
        "provider": resolution.provider,
        "model": resolution.model,
        "resolved_at": resolution.created_at,
        "exported_at": exported_at,
    }


def to_float(amount: Decimal | None) -> float | None:
    """Analytics tools aggregate REAL columns; the exact Decimal stays in the operational DB."""
    return float(amount) if amount is not None else None
