import sqlite3
from contextlib import closing
from typing import Any

import pytest

from app import storage
from app.config import Settings
from app.main import create_app
from app.normalization import normalize_invoice
from app.reference import CanonicalSupplier, ReferenceData
from app.schemas import InvoiceIn
from app.tools import ToolError, match_canonical_supplier, run_tool, search_history

# The supplier each seed invoice proves; None = new or unconfirmed supplier.
EXPECTED_PROVEN_MATCH = {
    "doc_new_001": ("ORANGE ESPAGNE SA", "alias"),
    "doc_new_002": ("GOOGLE CLOUD EMEA LTD", "tax_id"),
    "doc_new_003": ("GOOGLE ADS FRANCE", "tax_id"),
    "doc_new_004": ("STAPLES IBERIA SA", "tax_id"),
    "doc_new_005": ("ENDESA ENERGIA SAU", "tax_id"),
    "doc_new_006": ("IBERIA VIAJES CORPORATE SL", "alias"),
    "doc_new_007": ("DELOITTE ADVISORY SL", "tax_id"),
    "doc_new_008": ("MICROSOFT IRELAND OPERATIONS LTD", "tax_id"),
    "doc_new_009": ("UBER BV", "alias"),
    "doc_new_010": None,
    "doc_new_011": None,
    "doc_new_012": ("AWS EMEA SARL", "tax_id"),
    "doc_new_013": ("ORANGE ESPAGNE SA", "tax_id"),
    "doc_new_014": ("GOOGLE ADS FRANCE", "alias"),
    "doc_new_015": None,
}


def test_exact_tax_id_proves_the_supplier(reference: ReferenceData) -> None:
    match = match_canonical_supplier(reference, "Google C1oud EMEA", "IE6388047V")

    proven = match.proven_match()
    assert proven is not None
    assert (proven.canonical_supplier, proven.match_type, proven.score) == (
        "GOOGLE CLOUD EMEA LTD",
        "tax_id",
        1.0,
    )


def test_known_ocr_misspelling_matches_by_alias(reference: ReferenceData) -> None:
    match = match_canonical_supplier(reference, "StapIes Iberia")

    proven = match.proven_match()
    assert proven is not None
    assert (proven.canonical_supplier, proven.match_type, proven.matched_on) == (
        "STAPLES IBERIA SA",
        "alias",
        "StapIes Iberia",
    )


def test_amazon_is_only_a_fuzzy_hint_for_aws(reference: ReferenceData) -> None:
    match = match_canonical_supplier(reference, "Amazon")

    assert match.proven_match() is None
    assert [(c.canonical_supplier, c.match_type, c.score) for c in match.candidates] == [
        ("AWS EMEA SARL", "fuzzy", 0.8)
    ]


def test_tax_id_and_name_of_different_suppliers_prove_neither(reference: ReferenceData) -> None:
    # "Google Ads" is an alias of GOOGLE ADS FRANCE; IE6388047V is GOOGLE CLOUD EMEA LTD's tax ID.
    match = match_canonical_supplier(reference, "Google Ads", "IE6388047V")

    assert [c.canonical_supplier for c in match.exact_matches()] == [
        "GOOGLE CLOUD EMEA LTD",
        "GOOGLE ADS FRANCE",
    ]
    assert match.proven_match() is None
    assert (
        match.summary() == "conflict: GOOGLE CLOUD EMEA LTD by tax_id vs GOOGLE ADS FRANCE by alias"
    )


def test_alias_contradicted_by_another_tax_id_is_not_proof(reference: ReferenceData) -> None:
    match = match_canonical_supplier(reference, "Staples", "ESB12345678")

    assert match.proven_match() is None
    assert match.candidates[0].tax_id_differs
    assert match.summary() == (
        "best STAPLES IBERIA SA (alias 0.95, invoice tax ID differs), 1 candidate(s)"
    )


def test_candidates_are_ranked_by_match_type_before_score() -> None:
    def supplier(name: str, alias: str, tax_id: str) -> CanonicalSupplier:
        return CanonicalSupplier(
            canonical_supplier=name,
            country="ES",
            supplier_tax_id=tax_id,
            known_aliases=[alias],
            typical_category="other",
            typical_business_unit="general_admin",
        )

    reference = ReferenceData(
        categories=["other"],
        business_units=["general_admin"],
        canonical_suppliers=[
            supplier("ACME IBERIAA SA", "Acme Iberiaa", "ESB00000002"),
            supplier("ACME IBERIA SL", "Acme Iberia", "ESB00000001"),
        ],
        review_rules=[],
        country_hints=[],
    )

    match = match_canonical_supplier(reference, "Acme Iberia")

    # The similar name scores 0.96, above the alias's 0.95, but an exact alias ranks first.
    assert [(c.canonical_supplier, c.match_type, c.score) for c in match.candidates] == [
        ("ACME IBERIA SL", "alias", 0.95),
        ("ACME IBERIAA SA", "fuzzy", 0.96),
    ]


def test_unknown_supplier_has_no_candidates(reference: ReferenceData) -> None:
    match = match_canonical_supplier(reference, "WeWork Spain", "ESA12345000")

    assert match.candidates == []
    assert match.summary() == "no known supplier matches"


def test_every_seed_invoice_gets_the_expected_match(
    reference: ReferenceData, seed_invoices: list[dict[str, Any]]
) -> None:
    for item in seed_invoices:
        invoice, _ = normalize_invoice(InvoiceIn.model_validate(item))

        proven = match_canonical_supplier(
            reference, invoice.supplier_name, invoice.supplier_tax_id
        ).proven_match()

        found = (proven.canonical_supplier, proven.match_type) if proven else None
        assert found == EXPECTED_PROVEN_MATCH[invoice.document_id], invoice.document_id


def test_history_by_canonical_supplier_counts_categories(history_db: sqlite3.Connection) -> None:
    result = search_history(history_db, canonical_supplier="orange espagne sa")

    assert result.match_count == 6
    assert result.categories == {"telecom": 6}
    assert result.business_units == {"it": 6}
    assert result.review_decisions == {"auto_approve": 5, "needs_review": 1}
    dates = [row.resolved_at for row in result.recent_rows]
    assert dates == sorted(dates, reverse=True)
    assert result.summary().startswith("6 history rows | categories telecom x6")


def test_history_counts_cover_all_rows_not_only_the_recent_ones(
    history_db: sqlite3.Connection,
) -> None:
    # Orange's 6 rows by name plus Google Cloud's 5 rows by tax ID.
    result = search_history(
        history_db, canonical_supplier="ORANGE ESPAGNE SA", supplier_tax_id="IE6388047V"
    )

    assert result.match_count == 11
    assert len(result.recent_rows) == 10
    assert result.categories == {"telecom": 6, "software": 5}


def test_history_by_tax_id_or_raw_name(history_db: sqlite3.Connection) -> None:
    by_tax_id = search_history(history_db, supplier_tax_id="ESA82009812")
    by_name = search_history(history_db, supplier_name="Orange Espana")

    assert by_tax_id.canonical_suppliers == {"ORANGE ESPAGNE SA": 6}
    assert by_name.match_count == 1
    assert by_name.canonical_suppliers == {"ORANGE ESPAGNE SA": 1}


def test_history_for_a_new_supplier_is_empty(history_db: sqlite3.Connection) -> None:
    result = search_history(history_db, supplier_name="WeWork Spain", supplier_tax_id="ESA12345000")

    assert result.match_count == 0
    assert result.summary() == "no history rows"


def test_run_tool_dispatches_and_checks_arguments(
    history_db: sqlite3.Connection, reference: ReferenceData
) -> None:
    by_name = run_tool("match_canonical_supplier", {"supplier_name": "Uber"}, history_db, reference)
    by_tax_id = run_tool(
        "match_canonical_supplier",
        {"supplier_name": "", "supplier_tax_id": "ESA82009812"},
        history_db,
        reference,
    )

    assert by_name.summary() == "best UBER BV (alias 0.95), 1 candidate(s)"
    assert by_tax_id.summary() == "best ORANGE ESPAGNE SA (tax_id 1.00), 1 candidate(s)"


@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("delete_invoices", {}, "Unknown tool"),
        ("match_canonical_supplier", {}, "Give a supplier_name or a supplier_tax_id"),
        ("match_canonical_supplier", {"supplier_name": " . "}, "Give a supplier_name"),
        ("search_history", {}, "Give at least one"),
        ("search_history", {"supplier_name": "   "}, "Give at least one"),
        ("search_history", {"supplier_tax_id": 42}, "must be a string"),
    ],
)
def test_run_tool_rejects_unusable_calls(
    tool: str,
    arguments: dict[str, Any],
    message: str,
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    with pytest.raises(ToolError, match=message):
        run_tool(tool, arguments, history_db, reference)


def test_seeding_history_twice_adds_nothing(
    history_db: sqlite3.Connection, history_rows: list[dict[str, Any]]
) -> None:
    inserted = storage.seed_history(history_db, history_rows)

    assert inserted == 0
    assert history_db.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 51


def test_every_startup_loads_the_history_once(settings: Settings) -> None:
    create_app(settings)
    create_app(settings)

    with closing(storage.connect(settings.operational_db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM history").fetchone()[0] == 51
