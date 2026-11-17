"""HTTP endpoints. Each one calls the pipeline and maps the outcome to a status code."""

import sqlite3
from collections.abc import Iterator
from contextlib import closing
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app import export, pipeline, storage
from app.schemas import ExportResult, HealthStatus, ImportResult, InvoiceIn, InvoiceRecord

router = APIRouter()


def get_db(request: Request) -> Iterator[sqlite3.Connection]:
    """One connection to the operational database per request, closed after the response."""
    conn = storage.connect(request.app.state.settings.operational_db_path)
    try:
        yield conn
    finally:
        conn.close()


Database = Annotated[sqlite3.Connection, Depends(get_db)]


@router.get("/health")
def health(request: Request) -> HealthStatus:
    """Shows which LLM client is active, so nobody mistakes the fake for the real model."""
    llm = request.app.state.llm
    return HealthStatus(status="ok", llm_provider=llm.provider, llm_model=llm.model)


@router.post("/invoices", status_code=status.HTTP_201_CREATED)
def create_invoice(payload: InvoiceIn, conn: Database) -> InvoiceRecord:
    """Validate, normalize and store one post-OCR payload."""
    if not pipeline.ingest_invoice(conn, payload):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"Invoice {payload.document_id} already exists."
        )
    return get_invoice_or_404(conn, payload.document_id)


@router.post("/invoices/import")
def import_invoices(items: list[Any], conn: Database) -> ImportResult:
    """Ingest a JSON array of payloads; reports created, duplicate and invalid items."""
    return pipeline.import_invoices(conn, items)


@router.get("/invoices/{document_id}")
def get_invoice(document_id: str, conn: Database) -> InvoiceRecord:
    """The invoice with its payloads, flags, status and latest resolution (with its trace)."""
    return get_invoice_or_404(conn, document_id)


@router.post("/invoices/{document_id}/resolve")
def resolve_invoice(document_id: str, request: Request, conn: Database) -> InvoiceRecord:
    """Run the agent on the invoice. Every call creates a new resolution.

    Returns 200 also when the run failed: the response then has status `failed`, the error and
    the partial trace.
    """
    invoice = pipeline.resolve_invoice(
        conn, document_id, request.app.state.llm, request.app.state.reference
    )
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Invoice {document_id} not found.")
    return invoice


@router.post("/exports/run")
def run_export(request: Request, conn: Database) -> ExportResult:
    """Copy the resolutions not exported yet into the analytical database."""
    analytics_path = request.app.state.settings.analytics_db_path
    with closing(storage.connect(analytics_path)) as analytics:
        return export.run_export(conn, analytics)


def get_invoice_or_404(conn: sqlite3.Connection, document_id: str) -> InvoiceRecord:
    invoice = storage.get_invoice(conn, document_id)
    if invoice is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Invoice {document_id} not found.")
    return invoice
