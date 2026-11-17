"""The agent's tools: deterministic lookups over the reference data and the history.

Each tool is a plain function with a typed result. `run_tool` is the single entry point the
agent loop uses: it checks the model's arguments, dispatches by name and returns the result.
`TOOL_DEFINITIONS` is what the model sees: names, descriptions and input schemas.
"""

import sqlite3
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Literal

from pydantic import BaseModel

from app import storage
from app.normalization import normalize_tax_id, supplier_name_key
from app.reference import CanonicalSupplier, ReferenceData

TAX_ID_SCORE = 1.0
ALIAS_SCORE = 0.95
# Name similarity (difflib ratio) below this is not reported. "AMAZON" vs the AWS alias
# "AMAZON WS" scores exactly 0.80: reported as a candidate, but similarity never proves.
FUZZY_MIN_SCORE = 0.80
MAX_CANDIDATES = 3
MAX_HISTORY_ROWS = 10

MatchType = Literal["tax_id", "alias", "fuzzy"]
# Candidates are listed by match type first (tax ID beats alias beats similarity), then score.
MATCH_TYPE_RANK: dict[MatchType, int] = {"tax_id": 0, "alias": 1, "fuzzy": 2}


class ToolError(Exception):
    """A tool call with unusable arguments. The message goes back to the model."""


class SupplierCandidate(BaseModel):
    canonical_supplier: str
    match_type: MatchType
    score: float
    matched_on: str  # the tax ID or the alias spelling that matched
    tax_id_differs: bool  # the invoice has a tax ID, and it is not this supplier's
    supplier_tax_id: str
    country: str
    typical_category: str
    typical_business_unit: str

    @property
    def is_exact(self) -> bool:
        """An exact tax ID or an exact known alias. Name similarity is never exact."""
        return self.match_type in ("tax_id", "alias")


class SupplierMatch(BaseModel):
    supplier_name: str | None
    supplier_tax_id: str | None
    candidates: list[SupplierCandidate]

    def exact_matches(self) -> list[SupplierCandidate]:
        return [candidate for candidate in self.candidates if candidate.is_exact]

    def proven_match(self) -> SupplierCandidate | None:
        """The supplier the invoice proves, or None.

        Proof is one exact match: a tax ID, or an alias that the invoice's tax ID does not
        contradict. Each supplier gives at most one candidate, so two exact matches name two
        different suppliers (the tax ID of one, the alias of another): they prove neither.
        """
        exact = self.exact_matches()
        if len(exact) != 1:
            return None
        if exact[0].match_type == "alias" and exact[0].tax_id_differs:
            return None
        return exact[0]

    def summary(self) -> str:
        exact = self.exact_matches()
        if len(exact) > 1:
            names = " vs ".join(f"{c.canonical_supplier} by {c.match_type}" for c in exact)
            return f"conflict: {names}"
        if not self.candidates:
            return "no known supplier matches"
        best = self.candidates[0]
        tax_id_note = ", invoice tax ID differs" if best.tax_id_differs else ""
        return (
            f"best {best.canonical_supplier} ({best.match_type} {best.score:.2f}{tax_id_note}), "
            f"{len(self.candidates)} candidate(s)"
        )


class HistoryRow(BaseModel):
    history_id: str
    supplier_name_raw: str | None
    supplier_tax_id: str | None
    invoice_date: str | None
    description: str | None
    total_amount: float | None
    canonical_supplier: str
    predicted_spend_category: str
    predicted_business_unit: str
    review_decision: str
    confidence: float | None
    resolved_at: str | None


class HistorySearch(BaseModel):
    match_count: int
    canonical_suppliers: dict[str, int]
    categories: dict[str, int]
    business_units: dict[str, int]
    review_decisions: dict[str, int]
    recent_rows: list[HistoryRow]

    def summary(self) -> str:
        if self.match_count == 0:
            return "no history rows"
        return (
            f"{self.match_count} history rows | categories {format_counts(self.categories)}"
            f" | business units {format_counts(self.business_units)}"
            f" | decisions {format_counts(self.review_decisions)}"
        )


def match_canonical_supplier(
    reference: ReferenceData, supplier_name: str | None, supplier_tax_id: str | None = None
) -> SupplierMatch:
    """Compare the invoice's tax ID and name with every reference supplier."""
    name_key = supplier_name_key(supplier_name)
    tax_id = normalize_tax_id(supplier_tax_id)
    candidates = []
    for supplier in reference.canonical_suppliers:
        candidate = score_supplier(supplier, name_key, tax_id)
        if candidate is not None:
            candidates.append(candidate)
    candidates.sort(key=lambda c: (MATCH_TYPE_RANK[c.match_type], -c.score))
    return SupplierMatch(
        supplier_name=supplier_name, supplier_tax_id=tax_id, candidates=candidates[:MAX_CANDIDATES]
    )


def score_supplier(
    supplier: CanonicalSupplier, name_key: str | None, tax_id: str | None
) -> SupplierCandidate | None:
    if tax_id is not None and tax_id == supplier.supplier_tax_id:
        return make_candidate(supplier, "tax_id", TAX_ID_SCORE, tax_id, tax_id_differs=False)
    if name_key is None:
        return None
    # From here on, a tax ID on the invoice is not this supplier's.
    tax_id_differs = tax_id is not None
    variants = supplier.name_variants()
    if name_key in variants:
        return make_candidate(supplier, "alias", ALIAS_SCORE, variants[name_key], tax_id_differs)
    similarity, closest_key = max(
        (SequenceMatcher(None, name_key, key).ratio(), key) for key in variants
    )
    if similarity >= FUZZY_MIN_SCORE:
        return make_candidate(
            supplier, "fuzzy", round(similarity, 2), variants[closest_key], tax_id_differs
        )
    return None


def make_candidate(
    supplier: CanonicalSupplier,
    match_type: MatchType,
    score: float,
    matched_on: str,
    tax_id_differs: bool,
) -> SupplierCandidate:
    return SupplierCandidate(
        canonical_supplier=supplier.canonical_supplier,
        match_type=match_type,
        score=score,
        matched_on=matched_on,
        tax_id_differs=tax_id_differs,
        supplier_tax_id=supplier.supplier_tax_id,
        country=supplier.country,
        typical_category=supplier.typical_category,
        typical_business_unit=supplier.typical_business_unit,
    )


def search_history(
    conn: sqlite3.Connection,
    canonical_supplier: str | None = None,
    supplier_tax_id: str | None = None,
    supplier_name: str | None = None,
) -> HistorySearch:
    """Earlier resolutions that match the canonical name, the tax ID or the raw name.

    The counts cover every matching row; `recent_rows` holds only the newest ones.
    """
    canonical_key = supplier_name_key(canonical_supplier)
    tax_id = normalize_tax_id(supplier_tax_id)
    name_key = supplier_name_key(supplier_name)
    if canonical_key is None and tax_id is None and name_key is None:
        raise ToolError(
            "Give at least one of canonical_supplier, supplier_tax_id or supplier_name, "
            "with letters or digits."
        )
    rows = storage.find_history(conn, canonical_key=canonical_key, tax_id=tax_id, name_key=name_key)
    return HistorySearch(
        match_count=len(rows),
        canonical_suppliers=count_values(rows, "canonical_supplier"),
        categories=count_values(rows, "predicted_spend_category"),
        business_units=count_values(rows, "predicted_business_unit"),
        review_decisions=count_values(rows, "review_decision"),
        recent_rows=[HistoryRow.model_validate(dict(row)) for row in rows[:MAX_HISTORY_ROWS]],
    )


def count_values(rows: list[sqlite3.Row], column: str) -> dict[str, int]:
    """{'telecom': 6}: how often each value appears, most frequent first."""
    return dict(Counter(row[column] for row in rows).most_common())


def format_counts(counts: dict[str, int]) -> str:
    return ", ".join(f"{value} x{count}" for value, count in counts.items())


ToolResult = SupplierMatch | HistorySearch


def run_tool(
    name: str, tool_input: dict[str, Any], conn: sqlite3.Connection, reference: ReferenceData
) -> ToolResult:
    """Run the tool the model asked for. Raises ToolError for an unknown tool or bad input."""
    if name == "match_canonical_supplier":
        supplier_name = text_argument(tool_input, "supplier_name")
        supplier_tax_id = text_argument(tool_input, "supplier_tax_id")
        if supplier_name_key(supplier_name) is None and normalize_tax_id(supplier_tax_id) is None:
            raise ToolError("Give a supplier_name or a supplier_tax_id with letters or digits.")
        return match_canonical_supplier(reference, supplier_name, supplier_tax_id)
    if name == "search_history":
        return search_history(
            conn,
            canonical_supplier=text_argument(tool_input, "canonical_supplier"),
            supplier_tax_id=text_argument(tool_input, "supplier_tax_id"),
            supplier_name=text_argument(tool_input, "supplier_name"),
        )
    raise ToolError(f"Unknown tool: {name}")


def text_argument(tool_input: dict[str, Any], key: str) -> str | None:
    value = tool_input.get(key)
    if value is None or isinstance(value, str):
        return value
    raise ToolError(f"{key} must be a string.")


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "match_canonical_supplier",
        "description": (
            "Find which known canonical supplier an invoice comes from. Compares the invoice's "
            "tax ID and supplier name (case, accents and punctuation ignored) with the reference "
            "suppliers and their known aliases, OCR misspellings included. Returns up to "
            f"{MAX_CANDIDATES} candidates, best first: match_type 'tax_id' (same tax ID, score "
            f"{TAX_ID_SCORE}), 'alias' (same name as a known variant, score {ALIAS_SCORE}) or "
            f"'fuzzy' (similar name, score = similarity from {FUZZY_MIN_SCORE} to 1.0). The "
            "supplier is proven by a single tax_id or alias candidate; an alias does not prove "
            "it when tax_id_differs is true (the invoice carries another tax ID). If the tax ID "
            "and the name point to different suppliers, neither is proven. A fuzzy candidate is "
            "only a hint and may be a different company. An empty list means nothing matched "
            "by tax ID, alias or similar name: the supplier may be new or written very "
            "differently."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "supplier_name": {
                    "type": "string",
                    "description": "Supplier name exactly as written on the invoice.",
                },
                "supplier_tax_id": {
                    "type": "string",
                    "description": "Supplier tax ID from the invoice. Omit it if there is none.",
                },
            },
            "required": ["supplier_name"],
            "additionalProperties": False,
        },
    },
    {
        "name": "search_history",
        "description": (
            "Search previously resolved invoices. A row matches if its canonical supplier equals "
            "canonical_supplier, its tax ID equals supplier_tax_id, or its raw supplier name "
            "equals supplier_name (case, accents and punctuation ignored). Give at least one "
            "argument. Returns how many rows matched; how often each canonical supplier, "
            "category, business unit and review decision appears among all of them; and the "
            f"{MAX_HISTORY_ROWS} most recent rows. Use it to see how invoices from this supplier "
            "were classified before and whether the history is consistent."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "canonical_supplier": {
                    "type": "string",
                    "description": "Canonical supplier name, e.g. from match_canonical_supplier.",
                },
                "supplier_tax_id": {
                    "type": "string",
                    "description": "Supplier tax ID from the invoice.",
                },
                "supplier_name": {
                    "type": "string",
                    "description": "Supplier name as written on the invoice.",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]
