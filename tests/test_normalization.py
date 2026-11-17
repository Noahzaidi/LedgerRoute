from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.normalization import (
    is_ambiguous_date,
    normalize_invoice,
    normalize_tax_id,
    parse_amount,
    parse_date,
    supplier_name_key,
)
from app.schemas import InvoiceIn, RawAmount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026/03/02", date(2026, 3, 2)),
        ("2026-03-31", date(2026, 3, 31)),
        ("03-04-2026", date(2026, 4, 3)),  # day first
        ("31/12/2026", date(2026, 12, 31)),
        (" 2026-04-01 ", date(2026, 4, 1)),
        ("2026-02-30", None),
        ("not a date", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_date(raw: str | None, expected: date | None) -> None:
    assert parse_date(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("03-04-2026", True),  # 3 April or 4 March
        ("28-03-2026", False),  # only one valid reading
        ("04-04-2026", False),  # both readings are the same day
        ("2026-03-04", False),  # year first is never ambiguous
    ],
)
def test_is_ambiguous_date(raw: str, expected: bool) -> None:
    assert is_ambiguous_date(raw) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("82,64", Decimal("82.64")),
        ("1240.00", Decimal("1240.00")),
        ("1.240,50", Decimal("1240.50")),
        ("1,240.50", Decimal("1240.50")),
        ("€ 99,99", Decimal("99.99")),
        ("99,99 EUR", Decimal("99.99")),
        ("-12,50", Decimal("-12.50")),
        (1240, Decimal("1240")),
        (12.5, Decimal("12.5")),
        ("1O0.00", None),  # OCR read the letter O for a zero: unreadable, not 10.00
        ("12-50", None),
        ("abc", None),
        ("", None),
        (None, None),
    ],
)
def test_parse_amount(raw: RawAmount, expected: Decimal | None) -> None:
    assert parse_amount(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Orange Espagne, S.A.", "ORANGE ESPAGNE SA"),
        ("ORANGE ESPAGNE,S.A.", "ORANGE ESPAGNE SA"),  # punctuation between words splits them
        ("Iberia-Viajes", "IBERIA VIAJES"),
        ("McDonald's", "MCDONALDS"),
        ("Endesa Energía", "ENDESA ENERGIA"),
        ("  A.W.S.   EMEA ", "AWS EMEA"),
        ("", None),
        (None, None),
    ],
)
def test_supplier_name_key(raw: str | None, expected: str | None) -> None:
    assert supplier_name_key(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(" ESA-82009812 ", "ESA82009812"), ("ie 6388047v", "IE6388047V"), ("", None), (None, None)],
)
def test_normalize_tax_id(raw: str | None, expected: str | None) -> None:
    assert normalize_tax_id(raw) == expected


def test_orange_invoice_without_tax_id(seed_by_id: dict[str, dict[str, Any]]) -> None:
    raw = InvoiceIn.model_validate(seed_by_id["doc_new_001"])

    invoice, flags = normalize_invoice(raw)

    assert invoice.invoice_date == date(2026, 3, 2)
    assert (invoice.base_amount, invoice.tax_amount, invoice.total_amount) == (
        Decimal("82.64"),
        Decimal("17.35"),
        Decimal("99.99"),
    )
    assert invoice.supplier_tax_id is None
    assert invoice.supplier_name_key == "ORANGE ESPANA"
    # The tax ID is missing, so its low OCR confidence (0.35) is not flagged twice.
    assert flags == ["missing:supplier_tax_id"]


def test_ambiguous_date_is_flagged(seed_by_id: dict[str, dict[str, Any]]) -> None:
    raw = InvoiceIn.model_validate(seed_by_id["doc_new_004"])

    invoice, flags = normalize_invoice(raw)

    assert invoice.invoice_date == date(2026, 4, 3)
    assert "ambiguous_date" in flags


def test_low_ocr_confidence_is_flagged(seed_by_id: dict[str, dict[str, Any]]) -> None:
    raw = InvoiceIn.model_validate(seed_by_id["doc_new_010"])

    _, flags = normalize_invoice(raw)

    assert flags == ["missing:supplier_tax_id", "low_confidence:description"]


def test_every_seed_invoice_has_readable_dates_and_amounts(
    seed_invoices: list[dict[str, Any]],
) -> None:
    for item in seed_invoices:
        invoice, flags = normalize_invoice(InvoiceIn.model_validate(item))

        assert invoice.invoice_date is not None, item["document_id"]
        assert invoice.total_amount is not None, item["document_id"]
        assert not [flag for flag in flags if flag.startswith("invalid:")], item["document_id"]
        assert "amount_mismatch" not in flags, item["document_id"]


def test_garbage_values_become_flags_not_errors() -> None:
    raw = InvoiceIn(
        document_id="doc_garbage",
        supplier_name="   ",
        invoice_date="31/31/2026",
        currency="euros",
        base_amount="10",
        tax_amount="2",
        total_amount="abc",
        country="Spain",
    )

    invoice, flags = normalize_invoice(raw)

    assert invoice.supplier_name is None
    assert invoice.total_amount is None
    assert "missing:supplier_name" in flags
    assert "invalid:invoice_date" in flags
    assert "invalid:currency" in flags
    assert "invalid:total_amount" in flags
    assert "invalid:country" in flags
    assert "missing:description" in flags


@pytest.mark.parametrize(
    ("total", "mismatch"),
    [("12.00", False), ("12.01", False), ("12.02", True), ("11.98", True)],
)
def test_amount_mismatch_tolerates_one_cent(total: str, mismatch: bool) -> None:
    raw = InvoiceIn(document_id="doc_sum", base_amount="10", tax_amount="2,00", total_amount=total)

    _, flags = normalize_invoice(raw)

    assert ("amount_mismatch" in flags) is mismatch
