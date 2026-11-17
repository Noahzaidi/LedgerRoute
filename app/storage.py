"""SQLite access. Every SQL statement of the service lives in this module."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.normalization import normalize_tax_id, supplier_name_key
from app.schemas import InvoiceRecord, NormalizedInvoice, ResolutionRecord

OPERATIONAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    document_id        TEXT PRIMARY KEY,
    raw_payload        TEXT NOT NULL,  -- JSON, the payload as validated (extra fields kept)
    normalized_payload TEXT NOT NULL,  -- JSON, NormalizedInvoice
    quality_flags      TEXT NOT NULL,  -- JSON list, e.g. ["missing:supplier_tax_id"]
    status             TEXT NOT NULL,  -- normalized | resolved | failed
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    exported_at        TEXT            -- NULL until the current resolution is exported
);

-- Resolved invoices from data/historical_resolutions.json: the agent's memory.
CREATE TABLE IF NOT EXISTS history (
    history_id               TEXT PRIMARY KEY NOT NULL,  -- SQLite allows NULL keys otherwise
    document_id              TEXT,
    supplier_name_raw        TEXT,
    supplier_name_key        TEXT,  -- matching key of supplier_name_raw
    supplier_tax_id          TEXT,
    invoice_number           TEXT,
    invoice_date             TEXT,
    currency                 TEXT,
    base_amount              REAL,
    tax_amount               REAL,
    total_amount             REAL,
    description              TEXT,
    country                  TEXT,
    canonical_supplier       TEXT NOT NULL,
    canonical_supplier_key   TEXT NOT NULL,  -- matching key of canonical_supplier
    predicted_spend_category TEXT NOT NULL,
    predicted_business_unit  TEXT NOT NULL,
    review_decision          TEXT NOT NULL,
    confidence               REAL,
    decision_explanation     TEXT,
    resolved_at              TEXT
);

-- Every resolution attempt. The latest one per invoice is its current resolution.
CREATE TABLE IF NOT EXISTS resolutions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   TEXT NOT NULL REFERENCES invoices (document_id),
    status        TEXT NOT NULL,  -- resolved | failed
    result        TEXT,           -- JSON Resolution after the review rules; NULL if failed
    model_answer  TEXT,           -- JSON AgentAnswer as the model returned it; NULL if failed
    applied_rules TEXT NOT NULL,  -- JSON list of {rule_id, reason}
    trace         TEXT NOT NULL,  -- JSON list of tool calls
    error         TEXT,           -- why the run failed; NULL if resolved
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    model_turns   INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
"""

ANALYTICS_SCHEMA = """
-- One flat row per exported resolution: invoice fields next to the resolved business fields.
CREATE TABLE IF NOT EXISTS resolved_invoices (
    document_id              TEXT NOT NULL,
    resolution_id            INTEGER NOT NULL,
    invoice_number           TEXT,
    invoice_date             TEXT,
    supplier_name_raw        TEXT,
    supplier_tax_id          TEXT,
    description              TEXT,  -- the text the category is decided from
    country                  TEXT,
    currency                 TEXT,
    base_amount              REAL,
    tax_amount               REAL,
    total_amount             REAL,
    canonical_supplier       TEXT NOT NULL,
    is_new_supplier          INTEGER NOT NULL,  -- 0 or 1
    predicted_spend_category TEXT NOT NULL,
    predicted_business_unit  TEXT NOT NULL,
    review_decision          TEXT NOT NULL,
    confidence               REAL NOT NULL,
    decision_explanation     TEXT NOT NULL,
    applied_rules            TEXT NOT NULL,  -- rule ids, comma-separated; '' if none
    quality_flags            TEXT NOT NULL,  -- flags, comma-separated; '' if none
    tool_calls               INTEGER NOT NULL,
    provider                 TEXT NOT NULL,
    model                    TEXT NOT NULL,
    resolved_at              TEXT NOT NULL,
    exported_at              TEXT NOT NULL,
    PRIMARY KEY (document_id, resolution_id)
);

-- The latest exported resolution of each invoice (a re-resolved invoice has several rows
-- above). After a failed re-resolution it is still the last successful one.
CREATE VIEW IF NOT EXISTS latest_resolved_invoices AS
SELECT *
FROM resolved_invoices AS r
WHERE r.resolution_id = (
    SELECT MAX(resolution_id) FROM resolved_invoices WHERE document_id = r.document_id
);
"""

# History columns copied as they are from data/historical_resolutions.json.
HISTORY_SEED_COLUMNS = (
    "history_id",
    "document_id",
    "supplier_name_raw",
    "invoice_number",
    "invoice_date",
    "currency",
    "base_amount",
    "tax_amount",
    "total_amount",
    "description",
    "country",
    "canonical_supplier",
    "predicted_spend_category",
    "predicted_business_unit",
    "review_decision",
    "confidence",
    "decision_explanation",
    "resolved_at",
)


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection to a database file, creating its folder if needed.

    The API opens one connection per request. `check_same_thread=False` is needed because
    FastAPI may open it (in a dependency) and use it (in the endpoint) on different worker
    threads; the connection is never shared between requests.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_operational_db(conn: sqlite3.Connection) -> None:
    conn.executescript(OPERATIONAL_SCHEMA)


def init_analytics_db(conn: sqlite3.Connection) -> None:
    conn.executescript(ANALYTICS_SCHEMA)


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def insert_invoice(
    conn: sqlite3.Connection,
    raw_payload: dict[str, Any],
    invoice: NormalizedInvoice,
    quality_flags: list[str],
) -> bool:
    """Insert a new invoice. Returns False, and changes nothing, if the document_id exists."""
    now = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO invoices (document_id, raw_payload, normalized_payload, quality_flags,
                              status, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'normalized', ?, ?)
        ON CONFLICT (document_id) DO NOTHING
        """,
        (
            invoice.document_id,
            json.dumps(raw_payload, ensure_ascii=False),
            invoice.model_dump_json(),
            json.dumps(quality_flags),
            now,
            now,
        ),
    )
    return cursor.rowcount == 1


def get_invoice(conn: sqlite3.Connection, document_id: str) -> InvoiceRecord | None:
    row = conn.execute("SELECT * FROM invoices WHERE document_id = ?", (document_id,)).fetchone()
    if row is None:
        return None
    return InvoiceRecord(
        document_id=row["document_id"],
        status=row["status"],
        raw_payload=json.loads(row["raw_payload"]),
        normalized=NormalizedInvoice.model_validate_json(row["normalized_payload"]),
        quality_flags=json.loads(row["quality_flags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        exported_at=row["exported_at"],
        resolution=latest_resolution(conn, document_id),
    )


def insert_resolution(conn: sqlite3.Connection, document_id: str, record: ResolutionRecord) -> int:
    """Store one resolution attempt; returns its id."""
    data = record.model_dump(mode="json")
    cursor = conn.execute(
        """
        INSERT INTO resolutions (document_id, status, result, model_answer, applied_rules, trace,
                                 error, provider, model, model_turns, created_at)
        VALUES (:document_id, :status, :result, :model_answer, :applied_rules, :trace,
                :error, :provider, :model, :model_turns, :created_at)
        """,
        {
            **data,
            "document_id": document_id,
            "result": to_json(data["result"]),
            "model_answer": to_json(data["model_answer"]),
            "applied_rules": to_json(data["applied_rules"]),
            "trace": to_json(data["trace"]),
        },
    )
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite did not return the new resolution id")
    return cursor.lastrowid


def latest_resolution(conn: sqlite3.Connection, document_id: str) -> ResolutionRecord | None:
    row = conn.execute(
        "SELECT * FROM resolutions WHERE document_id = ? ORDER BY id DESC LIMIT 1",
        (document_id,),
    ).fetchone()
    if row is None:
        return None
    return ResolutionRecord.model_validate(
        {
            **dict(row),
            "result": from_json(row["result"]),
            "model_answer": from_json(row["model_answer"]),
            "applied_rules": from_json(row["applied_rules"]),
            "trace": from_json(row["trace"]),
        }
    )


def mark_new_resolution(conn: sqlite3.Connection, document_id: str, status: str) -> None:
    """Set the invoice's status after a resolution and mark it pending export again."""
    conn.execute(
        "UPDATE invoices SET status = ?, updated_at = ?, exported_at = NULL WHERE document_id = ?",
        (status, now_iso(), document_id),
    )


def invoices_pending_export(conn: sqlite3.Connection) -> list[InvoiceRecord]:
    """Resolved invoices whose current resolution has not been exported yet."""
    rows = conn.execute(
        """
        SELECT document_id FROM invoices
        WHERE status = 'resolved' AND exported_at IS NULL
        ORDER BY document_id
        """
    ).fetchall()
    invoices = [get_invoice(conn, row["document_id"]) for row in rows]
    return [invoice for invoice in invoices if invoice is not None]


def mark_exported(conn: sqlite3.Connection, document_ids: list[str], exported_at: str) -> None:
    conn.executemany(
        "UPDATE invoices SET exported_at = ? WHERE document_id = ?",
        [(exported_at, document_id) for document_id in document_ids],
    )


def insert_analytics_rows(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> None:
    """Write flat rows to the analytical table. A row already there is replaced, not doubled."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO resolved_invoices (
            document_id, resolution_id, invoice_number, invoice_date, supplier_name_raw,
            supplier_tax_id, description, country, currency, base_amount, tax_amount,
            total_amount, canonical_supplier, is_new_supplier, predicted_spend_category,
            predicted_business_unit, review_decision, confidence, decision_explanation,
            applied_rules, quality_flags, tool_calls, provider, model, resolved_at, exported_at
        ) VALUES (
            :document_id, :resolution_id, :invoice_number, :invoice_date, :supplier_name_raw,
            :supplier_tax_id, :description, :country, :currency, :base_amount, :tax_amount,
            :total_amount, :canonical_supplier, :is_new_supplier, :predicted_spend_category,
            :predicted_business_unit, :review_decision, :confidence, :decision_explanation,
            :applied_rules, :quality_flags, :tool_calls, :provider, :model, :resolved_at,
            :exported_at
        )
        """,
        rows,
    )


def to_json(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False)


def from_json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def seed_history(conn: sqlite3.Connection, rows: list[dict[str, Any]]) -> int:
    """Load resolved-invoice history. Safe at every startup: existing history_ids are skipped.

    Returns how many rows were new. The matching keys are stored next to the names, so every
    name comparison in the service uses the same `supplier_name_key` rule. They are computed
    when a row is first inserted: after changing `supplier_name_key` or `normalize_tax_id`,
    delete the whole var/ folder. Deleting only operational.db would restart the resolution
    ids at 1, and they would clash with the rows already in analytics.db.
    """
    inserted = 0
    for row in rows:
        cursor = conn.execute(
            """
            INSERT INTO history (
                history_id, document_id, supplier_name_raw, supplier_name_key, supplier_tax_id,
                invoice_number, invoice_date, currency, base_amount, tax_amount, total_amount,
                description, country, canonical_supplier, canonical_supplier_key,
                predicted_spend_category, predicted_business_unit, review_decision, confidence,
                decision_explanation, resolved_at
            ) VALUES (
                :history_id, :document_id, :supplier_name_raw, :supplier_name_key,
                :supplier_tax_id, :invoice_number, :invoice_date, :currency, :base_amount,
                :tax_amount, :total_amount, :description, :country, :canonical_supplier,
                :canonical_supplier_key, :predicted_spend_category, :predicted_business_unit,
                :review_decision, :confidence, :decision_explanation, :resolved_at
            )
            ON CONFLICT (history_id) DO NOTHING
            """,
            {
                **{column: row.get(column) for column in HISTORY_SEED_COLUMNS},
                "history_id": row["history_id"],  # required: it makes seeding idempotent
                "supplier_name_key": supplier_name_key(row.get("supplier_name_raw")),
                "supplier_tax_id": normalize_tax_id(row.get("supplier_tax_id")),
                "canonical_supplier_key": supplier_name_key(row["canonical_supplier"]),
            },
        )
        inserted += cursor.rowcount
    conn.commit()
    return inserted


def find_history(
    conn: sqlite3.Connection,
    canonical_key: str | None,
    tax_id: str | None,
    name_key: str | None,
) -> list[sqlite3.Row]:
    """History rows matching any of the given keys, newest first. A None key matches nothing."""
    return conn.execute(
        """
        SELECT * FROM history
        WHERE canonical_supplier_key = :canonical_key
           OR supplier_tax_id = :tax_id
           OR supplier_name_key = :name_key
        ORDER BY resolved_at DESC, history_id DESC
        """,
        {"canonical_key": canonical_key, "tax_id": tax_id, "name_key": name_key},
    ).fetchall()
