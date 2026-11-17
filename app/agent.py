"""The resolution agent: a bounded loop of model turns and tool calls.

The model receives the normalized invoice, two lookup tools and the review rules. It must call
the tools and then answer with one JSON object. This module runs the loop and records every
tool call; the review rules are applied afterwards, in `review_rules.py`.
"""

import json
import logging
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, get_args

from app.llm import LLMClient, LLMError, ToolCall
from app.normalization import supplier_name_key
from app.reference import ReferenceData
from app.schemas import AgentAnswer, NormalizedInvoice, ReviewDecision, TraceStep
from app.tools import TOOL_DEFINITIONS, ToolError, run_tool

logger = logging.getLogger(__name__)

MAX_TURNS = 6

SYSTEM_PROMPT = """\
You resolve business fields for supplier invoices. An OCR step has already extracted the fields
and a backend has cleaned them. You receive the cleaned invoice as JSON, with data-quality flags
such as missing:supplier_tax_id or low_confidence:description.

Work in this order:
1. Call match_canonical_supplier with the supplier name and, if present, the tax ID.
2. Call search_history for the canonical supplier you found, or with the raw supplier name and
   tax ID if nothing matched, to see how earlier invoices were classified.
3. Only then give your final answer. The JSON output format applies only to this final answer.

Fields of the final answer:
- canonical_supplier: if the supplier is proven (a single tax_id or alias candidate, see the tool
  description), use its canonical_supplier exactly as returned. A fuzzy candidate is only a
  hint. Otherwise write the supplier name in upper case without punctuation; it is then a new
  or unconfirmed supplier.
- predicted_spend_category: one of {categories}.
- predicted_business_unit: one of {business_units}.
- review_decision: auto_approve only when the supplier is proven, the history is consistent and
  no review rule applies; otherwise needs_review.
- confidence: from 0.0 to 1.0, how sure you are about the whole resolution. Use about 0.85 to
  0.95 for a proven supplier with consistent history, and 0.6 or less when the case needs review.
- decision_explanation: one or two short sentences in English for a finance reviewer that name
  the evidence: how the supplier matched, what the history shows, which rule applies.

Review rules:
{rules}
This service reads RR4 strictly: a supplier that is not proven always needs review.

Country notes:
{hints}
"""


@dataclass
class AgentRun:
    """What one agent run produced. `answer` is None when the run failed; `error` says why."""

    answer: AgentAnswer | None
    trace: list[TraceStep]
    error: str | None
    turns: int


def run_agent(
    invoice: NormalizedInvoice,
    quality_flags: list[str],
    llm: LLMClient,
    conn: sqlite3.Connection,
    reference: ReferenceData,
) -> AgentRun:
    document_id = invoice.document_id
    system = build_system_prompt(reference)
    answer_schema = build_answer_schema(reference)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": build_user_message(invoice, quality_flags)}
    ]
    trace: list[TraceStep] = []
    logger.info("%s | agent start | provider=%s model=%s", document_id, llm.provider, llm.model)

    for turn in range(1, MAX_TURNS + 1):
        try:
            reply = llm.complete(system, messages, TOOL_DEFINITIONS, answer_schema)
        except LLMError as error:
            return failed_run(document_id, trace, turn, f"LLM call failed: {error}")

        if reply.stop_reason == "tool_use":
            # The assistant turn goes back unchanged (thinking blocks included), then all the
            # tool results together in one user message.
            messages.append({"role": "assistant", "content": reply.raw_content})
            results = []
            for call in reply.tool_calls:
                step, result_block = run_tool_call(document_id, turn, call, conn, reference)
                trace.append(step)
                results.append(result_block)
            messages.append({"role": "user", "content": results})
            continue

        # Refusal, max_tokens or any other stop: never use that turn's content.
        if reply.stop_reason != "end_turn":
            return failed_run(
                document_id, trace, turn, f"Model stopped with stop_reason={reply.stop_reason}."
            )
        if not trace:
            return failed_run(document_id, trace, turn, "Model answered without calling a tool.")
        try:
            answer = parse_answer(reply.text, reference)
        except ValueError as error:  # invalid JSON, schema violation or value outside a catalog
            return failed_run(document_id, trace, turn, f"Invalid final answer: {error}")

        logger.info(
            "%s | turn %d | answer | %s, %s, %s, %s, confidence %.2f",
            document_id,
            turn,
            answer.canonical_supplier,
            answer.predicted_spend_category,
            answer.predicted_business_unit,
            answer.review_decision,
            answer.confidence,
        )
        return AgentRun(answer=answer, trace=trace, error=None, turns=turn)

    return failed_run(document_id, trace, MAX_TURNS, f"No answer after {MAX_TURNS} model turns.")


def run_tool_call(
    document_id: str,
    turn: int,
    call: ToolCall,
    conn: sqlite3.Connection,
    reference: ReferenceData,
) -> tuple[TraceStep, dict[str, Any]]:
    """Run one tool call. Returns its trace step and the tool_result block for the model."""
    started = time.perf_counter()
    try:
        result = run_tool(call.name, call.input, conn, reference)
    except ToolError as error:
        # Sent back as an error result so the model can correct itself.
        output: dict[str, Any] = {"error": str(error)}
        summary = f"error: {error}"
        is_error = True
    else:
        output = result.model_dump(mode="json")
        summary = result.summary()
        is_error = False
    duration_ms = round((time.perf_counter() - started) * 1000)

    logger.info(
        "%s | turn %d | tool %s %s -> %s (%d ms)",
        document_id,
        turn,
        call.name,
        json.dumps(call.input, ensure_ascii=False),
        summary,
        duration_ms,
    )
    step = TraceStep(
        turn=turn,
        tool=call.name,
        input=call.input,
        output=output,
        summary=summary,
        is_error=is_error,
        duration_ms=duration_ms,
    )
    result_block = {
        "type": "tool_result",
        "tool_use_id": call.id,
        "content": json.dumps(output, ensure_ascii=False),
        "is_error": is_error,
    }
    return step, result_block


def failed_run(document_id: str, trace: list[TraceStep], turns: int, reason: str) -> AgentRun:
    logger.warning("%s | agent failed | %s", document_id, reason)
    return AgentRun(answer=None, trace=trace, error=reason, turns=turns)


def parse_answer(text: str, reference: ReferenceData) -> AgentAnswer:
    """Validate the final JSON again in code; the catalogs come from the reference data."""
    answer = AgentAnswer.model_validate_json(text)
    # The review rules look the supplier up by its matching key, so it must have one.
    if supplier_name_key(answer.canonical_supplier) is None:
        raise ValueError("canonical_supplier has no letters or digits")
    if answer.predicted_spend_category not in reference.categories:
        raise ValueError(f"category {answer.predicted_spend_category!r} is not in the catalog")
    if answer.predicted_business_unit not in reference.business_units:
        raise ValueError(f"business unit {answer.predicted_business_unit!r} is not in the catalog")
    return answer


def build_system_prompt(reference: ReferenceData) -> str:
    rules = [f"- {rule.rule_id}: {rule.description}" for rule in reference.review_rules]
    hints = [f"- {hint.country}: {hint.notes}" for hint in reference.country_hints]
    return SYSTEM_PROMPT.format(
        categories=", ".join(reference.categories),
        business_units=", ".join(reference.business_units),
        rules="\n".join(rules),
        hints="\n".join(hints),
    )


def build_answer_schema(reference: ReferenceData) -> dict[str, Any]:
    """The JSON schema of the final answer. The API enforces it (structured output), and the
    enums keep the category and the business unit inside the closed lists."""
    return {
        "type": "object",
        "properties": {
            "canonical_supplier": {
                "type": "string",
                "description": "Canonical supplier name exactly as a tool returned it, or the "
                "supplier name in upper case for a new supplier.",
            },
            "predicted_spend_category": {"type": "string", "enum": reference.categories},
            "predicted_business_unit": {"type": "string", "enum": reference.business_units},
            "review_decision": {"type": "string", "enum": list(get_args(ReviewDecision))},
            "confidence": {"type": "number", "description": "From 0.0 to 1.0."},
            "decision_explanation": {
                "type": "string",
                "description": "One or two short sentences in English for a finance reviewer.",
            },
        },
        "required": [
            "canonical_supplier",
            "predicted_spend_category",
            "predicted_business_unit",
            "review_decision",
            "confidence",
            "decision_explanation",
        ],
        "additionalProperties": False,
    }


def build_user_message(invoice: NormalizedInvoice, quality_flags: list[str]) -> str:
    """The invoice as JSON: what the agent resolves, plus the flags from normalization."""
    payload = {"invoice": invoice.model_dump(mode="json"), "quality_flags": quality_flags}
    return json.dumps(payload, ensure_ascii=False, indent=2)
