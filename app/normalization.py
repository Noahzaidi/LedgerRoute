"""Deterministic cleaning of post-OCR payloads.

No LLM here: every rule has one right answer. `normalize_invoice` never raises on bad values;
it returns the cleaned invoice plus data-quality flags such as `missing:supplier_tax_id`,
`invalid:invoice_date`, `low_confidence:description`, `ambiguous_date` or `amount_mismatch`.
"""

import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.schemas import InvoiceIn, NormalizedInvoice, RawAmount

# Same threshold as review rule RR2 in the reference data.
LOW_CONFIDENCE_THRESHOLD = 0.60
AMOUNT_TOLERANCE = Decimal("0.01")

# Whitespace and currency marks are dropped from an amount; what is left must be a number.
CURRENCY_MARKS = re.compile(r"\s|€|\$|£|EUR|USD|GBP", re.IGNORECASE)
AMOUNT_TEXT = re.compile(r"-?[\d.,]+")

# Year-first formats cannot be confused. Day-first is the European convention of the seed:
# "03-04-2026" is 3 April 2026.
YEAR_FIRST_FORMATS = ("%Y-%m-%d", "%Y/%m/%d")
DAY_FIRST_FORMATS = ("%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y")

# Fields that get a `missing:` or `invalid:` flag when they are empty or unreadable.
CHECKED_FIELDS = (
    "supplier_name",
    "supplier_tax_id",
    "invoice_number",
    "invoice_date",
    "currency",
    "base_amount",
    "tax_amount",
    "total_amount",
    "description",
    "country",
)


def normalize_invoice(raw: InvoiceIn) -> tuple[NormalizedInvoice, list[str]]:
    """Clean one payload and list its data-quality problems."""
    invoice = NormalizedInvoice(
        document_id=raw.document_id,
        supplier_name=clean_text(raw.supplier_name),
        supplier_name_key=supplier_name_key(raw.supplier_name),
        supplier_tax_id=normalize_tax_id(raw.supplier_tax_id),
        invoice_number=clean_text(raw.invoice_number),
        invoice_date=parse_date(raw.invoice_date),
        currency=normalize_code(raw.currency, length=3),
        base_amount=parse_amount(raw.base_amount),
        tax_amount=parse_amount(raw.tax_amount),
        total_amount=parse_amount(raw.total_amount),
        description=clean_text(raw.description),
        country=normalize_code(raw.country, length=2),
        raw_ocr_text=clean_text(raw.raw_ocr_text),
        field_confidence=raw.field_confidence or {},
    )
    return invoice, quality_flags(raw, invoice)


def quality_flags(raw: InvoiceIn, invoice: NormalizedInvoice) -> list[str]:
    flags: list[str] = []
    for field in CHECKED_FIELDS:
        if is_blank(getattr(raw, field)):
            flags.append(f"missing:{field}")
        elif getattr(invoice, field) is None:
            flags.append(f"invalid:{field}")

    if invoice.invoice_date is not None and is_ambiguous_date(raw.invoice_date):
        flags.append("ambiguous_date")
    if amounts_mismatch(invoice):
        flags.append("amount_mismatch")

    for field, confidence in sorted(invoice.field_confidence.items()):
        # A missing value already has its own flag; its OCR confidence adds nothing.
        if confidence < LOW_CONFIDENCE_THRESHOLD and f"missing:{field}" not in flags:
            flags.append(f"low_confidence:{field}")
    return flags


def is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def clean_text(value: str | None) -> str | None:
    """Trim and collapse whitespace; an empty string becomes None."""
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def supplier_name_key(name: str | None) -> str | None:
    """Key used to compare supplier names: 'Orange Espagne, S.A.' -> 'ORANGE ESPAGNE SA'.

    Dots and apostrophes are dropped ('S.A.' -> 'SA'); any other punctuation separates words
    ('Iberia-Viajes' -> 'IBERIA VIAJES', 'ESPAGNE,S.A.' -> 'ESPAGNE SA').
    """
    text = clean_text(name)
    if text is None:
        return None
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    without_dots = re.sub(r"[.'’]", "", without_accents)
    words = re.sub(r"[^\w\s]", " ", without_dots)
    key = " ".join(words.upper().split())
    return key or None


def normalize_tax_id(value: str | None) -> str | None:
    """'ESA-82009812 ' -> 'ESA82009812'; an empty value becomes None."""
    text = clean_text(value)
    if text is None:
        return None
    compact = re.sub(r"[\s.\-]", "", text).upper()
    return compact or None


def normalize_code(value: str | None, length: int) -> str | None:
    """Upper-case an ISO code ('eur' -> 'EUR'); None if it is not `length` ASCII letters."""
    text = clean_text(value)
    if text is None:
        return None
    code = text.upper()
    if len(code) == length and code.isascii() and code.isalpha():
        return code
    return None


def parse_date(value: str | None) -> date | None:
    text = clean_text(value)
    if text is None:
        return None
    for date_format in YEAR_FIRST_FORMATS + DAY_FIRST_FORMATS:
        try:
            return datetime.strptime(text, date_format).date()
        except ValueError:
            continue
    return None


def is_ambiguous_date(value: str | None) -> bool:
    """True for a day-first date that is also a valid month-first date, like '03-04-2026'."""
    text = clean_text(value)
    if text is None:
        return False
    for date_format in DAY_FIRST_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        return parsed.day <= 12 and parsed.day != parsed.month
    return False


def parse_amount(value: RawAmount) -> Decimal | None:
    """Parse '82,64', '1240.00', '1.240,50', '€ 99,99', '99,99 EUR' or a number.

    Returns None if the text is unreadable, for example an OCR letter in place of a digit
    ('1O0.00'): guessing would store a wrong amount. When both ',' and '.' appear, the last one
    is the decimal separator.
    """
    if value is None:
        return None
    if isinstance(value, int | float):
        text = str(value)
    else:
        text = CURRENCY_MARKS.sub("", value)
        if not AMOUNT_TEXT.fullmatch(text):
            return None
        if "," in text and "." in text:
            thousands_separator = "." if text.rfind(",") > text.rfind(".") else ","
            text = text.replace(thousands_separator, "")
        text = text.replace(",", ".")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def amounts_mismatch(invoice: NormalizedInvoice) -> bool:
    """True when base + tax differs from the total by more than one cent."""
    if invoice.base_amount is None or invoice.tax_amount is None or invoice.total_amount is None:
        return False
    difference = invoice.base_amount + invoice.tax_amount - invoice.total_amount
    return abs(difference) > AMOUNT_TOLERANCE
