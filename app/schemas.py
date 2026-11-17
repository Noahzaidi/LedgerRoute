"""Pydantic models: the shape of the data at each boundary (API, storage, agent)."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
)

# Amounts arrive as OCR strings ("82,64") or as numbers; normalization parses both.
# Strict types so that `true` is not read as the number 1.
RawAmount = StrictStr | StrictInt | StrictFloat | None

# An OCR confidence is a probability. NaN, infinity or a percentage (35) is rejected with 422:
# such a value cannot be compared with the 0.60 threshold.
Confidence = Annotated[float, Field(ge=0.0, le=1.0)]

InvoiceStatus = Literal["normalized", "resolved", "failed"]


class InvoiceIn(BaseModel):
    """A post-OCR payload as it arrives.

    Only `document_id` is required. A value of the wrong JSON type (an object where text is
    expected, a confidence outside 0..1) is rejected with 422. Any other value is kept, readable
    or not, and checked by the normalization step, which turns problems into quality flags.
    """

    # Numbers are accepted where text is expected: OCR may emit an invoice number as 12345.
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    # Also a URL path segment (/invoices/{document_id}), so no slashes or spaces.
    document_id: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, pattern=r"^[A-Za-z0-9_.\-]+$")
    ]
    supplier_name: str | None = None
    supplier_tax_id: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    currency: str | None = None
    base_amount: RawAmount = None
    tax_amount: RawAmount = None
    total_amount: RawAmount = None
    description: str | None = None
    country: str | None = None
    raw_ocr_text: str | None = None
    field_confidence: dict[str, Confidence] | None = None


class NormalizedInvoice(BaseModel):
    """The invoice after deterministic cleaning. This is what the agent receives."""

    document_id: str
    supplier_name: str | None
    supplier_name_key: str | None = Field(
        description="Upper case, without accents or punctuation; used to match supplier names."
    )
    supplier_tax_id: str | None
    invoice_number: str | None
    invoice_date: date | None
    currency: str | None
    base_amount: Decimal | None
    tax_amount: Decimal | None
    total_amount: Decimal | None
    description: str | None
    country: str | None
    raw_ocr_text: str | None
    field_confidence: dict[str, float]


ReviewDecision = Literal["auto_approve", "needs_review"]
NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class AgentAnswer(BaseModel):
    """The JSON object the model must return as its final answer.

    agent.parse_answer also checks that the supplier name has a matching key and that the
    category and business unit are in the catalogs.
    """

    canonical_supplier: NonEmptyText
    predicted_spend_category: str
    predicted_business_unit: str
    review_decision: ReviewDecision
    confidence: float = Field(ge=0.0, le=1.0)
    decision_explanation: NonEmptyText


class Resolution(AgentAnswer):
    """The final resolution: the model's answer after the review rules."""

    is_new_supplier: bool


class AppliedRule(BaseModel):
    rule_id: str
    reason: str


class TraceStep(BaseModel):
    """One tool call made during a resolution: what the model asked and what it got back."""

    turn: int
    tool: str
    input: dict[str, Any]
    output: dict[str, Any]
    summary: str
    is_error: bool
    duration_ms: int


class ResolutionRecord(BaseModel):
    """One resolution attempt, as stored and returned by the API."""

    id: int | None = None  # assigned by the database
    status: Literal["resolved", "failed"]
    result: Resolution | None  # None when the run failed
    model_answer: AgentAnswer | None  # what the model said, before the review rules
    applied_rules: list[AppliedRule]
    trace: list[TraceStep]
    error: str | None  # why the run failed
    provider: str
    model: str
    model_turns: int
    created_at: str


class InvoiceRecord(BaseModel):
    """An invoice as stored in the operational database and returned by the API."""

    document_id: str
    status: InvoiceStatus
    raw_payload: dict[str, Any]
    normalized: NormalizedInvoice
    quality_flags: list[str]
    created_at: str
    updated_at: str
    exported_at: str | None
    resolution: ResolutionRecord | None  # the latest one


class HealthStatus(BaseModel):
    status: Literal["ok"]
    llm_provider: str
    llm_model: str


class ExportResult(BaseModel):
    exported: int
    document_ids: list[str]
    exported_at: str


class InvalidItem(BaseModel):
    index: int
    document_id: str | None
    error: str


class ImportResult(BaseModel):
    created: list[str]
    duplicates: list[str]
    invalid: list[InvalidItem]
