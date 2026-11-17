"""Review rules applied in code after the model answers.

The model proposes; these rules guarantee the reference data's review policy whatever the model
said. They use evidence the code gathers itself with the same tool functions, so they do not
depend on which tools the model chose to call.
"""

import sqlite3
from dataclasses import dataclass

from app.reference import ReferenceData
from app.schemas import AgentAnswer, AppliedRule, NormalizedInvoice, Resolution
from app.tools import (
    HistorySearch,
    SupplierMatch,
    format_counts,
    match_canonical_supplier,
    search_history,
)

LOW_CONFIDENCE_FIELDS = ("supplier_name", "description")  # the fields named by RR2
MIN_AUTO_APPROVE_CONFIDENCE = 0.70
REVIEW_CONFIDENCE_CAP = 0.60


@dataclass
class Evidence:
    match: SupplierMatch  # for the invoice's own supplier name and tax ID
    history: HistorySearch  # for the canonical supplier the model chose


def gather_evidence(
    invoice: NormalizedInvoice,
    answer: AgentAnswer,
    conn: sqlite3.Connection,
    reference: ReferenceData,
) -> Evidence:
    return Evidence(
        match=match_canonical_supplier(reference, invoice.supplier_name, invoice.supplier_tax_id),
        history=search_history(conn, canonical_supplier=answer.canonical_supplier),
    )


def apply_review_rules(
    answer: AgentAnswer,
    quality_flags: list[str],
    evidence: Evidence,
    reference: ReferenceData,
) -> tuple[Resolution, list[AppliedRule]]:
    """The final resolution: the model's answer, corrected where a rule applies."""
    # A known supplier written differently ("Orange Espagne SA") gets its exact canonical name.
    known_supplier = reference.find_supplier(answer.canonical_supplier)
    canonical = known_supplier.canonical_supplier if known_supplier else answer.canonical_supplier
    proven = evidence.match.proven_match()

    rules: list[AppliedRule] = []
    if "missing:supplier_tax_id" in quality_flags and proven is None:
        rules.append(AppliedRule(rule_id="RR1", reason="No tax ID and no proven supplier."))
    low_fields = [f for f in LOW_CONFIDENCE_FIELDS if f"low_confidence:{f}" in quality_flags]
    if low_fields:
        rules.append(
            AppliedRule(
                rule_id="RR2", reason=f"OCR confidence below 0.60 for {' and '.join(low_fields)}."
            )
        )
    conflict = find_conflict(canonical, answer, evidence)
    if conflict is not None:
        rules.append(AppliedRule(rule_id="RR3", reason=conflict))
    unproven = unproven_reason(evidence.match)
    if unproven is not None:
        rules.append(AppliedRule(rule_id="RR4", reason=unproven))
    if answer.review_decision == "auto_approve" and answer.confidence < MIN_AUTO_APPROVE_CONFIDENCE:
        rules.append(
            AppliedRule(
                rule_id="LOW_CONFIDENCE",
                reason=f"Model confidence {answer.confidence:.2f} is below "
                f"{MIN_AUTO_APPROVE_CONFIDENCE:.2f} for auto-approval.",
            )
        )

    resolution = Resolution(
        canonical_supplier=canonical,
        predicted_spend_category=answer.predicted_spend_category,
        predicted_business_unit=answer.predicted_business_unit,
        review_decision="needs_review" if rules else answer.review_decision,
        confidence=min(answer.confidence, REVIEW_CONFIDENCE_CAP) if rules else answer.confidence,
        decision_explanation=explain(answer.decision_explanation, rules),
        is_new_supplier=known_supplier is None,
    )
    return resolution, rules


def find_conflict(canonical: str, answer: AgentAnswer, evidence: Evidence) -> str | None:
    """RR3: does the evidence disagree with itself, or the answer with the evidence?"""
    exact = evidence.match.exact_matches()
    if len(exact) > 1:
        names = ", ".join(f"{c.canonical_supplier} ({c.match_type})" for c in exact)
        return f"The tax ID and the name point to different suppliers: {names}."
    proven = evidence.match.proven_match()
    if proven is not None and proven.canonical_supplier != canonical:
        return (
            f"The answer names {canonical}, but the {proven.match_type} match is "
            f"{proven.canonical_supplier}."
        )
    history = evidence.history
    if len(history.categories) > 1:
        return (
            f"History for {canonical} has several categories: {format_counts(history.categories)}."
        )
    if len(history.business_units) > 1:
        return (
            f"History for {canonical} has several business units: "
            f"{format_counts(history.business_units)}."
        )
    if history.categories and answer.predicted_spend_category not in history.categories:
        return (
            f"History files {canonical} under {next(iter(history.categories))}, "
            f"the answer says {answer.predicted_spend_category}."
        )
    if history.business_units and answer.predicted_business_unit not in history.business_units:
        return (
            f"History assigns {canonical} to {next(iter(history.business_units))}, "
            f"the answer says {answer.predicted_business_unit}."
        )
    return None


def unproven_reason(match: SupplierMatch) -> str | None:
    """RR4: why the supplier is not proven, or None if it is (or if RR3 covers a conflict)."""
    exact = match.exact_matches()
    if match.proven_match() is not None or len(exact) > 1:
        return None
    if exact:  # a single alias, contradicted by the invoice's tax ID
        return (
            f"The name matches {exact[0].canonical_supplier}, but the invoice's tax ID is not "
            "that supplier's."
        )
    return "The supplier is not proven by its tax ID or a known alias: new or unconfirmed."


def explain(model_explanation: str, rules: list[AppliedRule]) -> str:
    if not rules:
        return model_explanation
    applied = " ".join(f"{rule.rule_id}: {rule.reason}" for rule in rules)
    return f"{model_explanation} Review rules applied: {applied}"
