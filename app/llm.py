"""LLM clients behind one small interface.

The agent only calls `complete()`. `AnthropicClient` sends the request to Claude. `FakeClient` is
a deterministic stand-in that calls the same tools and answers from their results, so the tests
and local integrations can run the whole pipeline without an API key.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import MessageParam, TextBlock, ToolParam, ToolUseBlock

from app.config import Effort, Settings
from app.tools import HistorySearch, SupplierMatch

logger = logging.getLogger(__name__)

# Thinking is always on for claude-opus-5-5 and counts toward max_tokens.
MAX_TOKENS = 16000
REQUEST_TIMEOUT_SECONDS = 120.0
MAX_RETRIES = 2


class LLMError(Exception):
    """The model could not be called: network, authentication, rate limit or server error."""


class ConfigurationError(Exception):
    """The settings do not allow building an LLM client."""


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class ModelTurn:
    """One model response, reduced to what the agent loop needs."""

    stop_reason: str
    text: str
    tool_calls: list[ToolCall]
    raw_content: list[Any]  # sent back unchanged as the next assistant message


class LLMClient(Protocol):
    provider: str
    model: str

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        answer_schema: dict[str, Any],
    ) -> ModelTurn: ...


class AnthropicClient:
    """Claude through the official SDK: Messages API with tools and a JSON-schema answer."""

    provider = "anthropic"

    def __init__(self, api_key: str, model: str, effort: Effort) -> None:
        self.model = model
        self.effort = effort
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES
        )

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        answer_schema: dict[str, Any],
    ) -> ModelTurn:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=system,
                messages=cast(list[MessageParam], messages),
                tools=cast(list[ToolParam], tools),
                # The format constrains only the final text answer; tool calls are unaffected.
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": answer_schema},
                },
            )
        except anthropic.APIError as error:
            raise LLMError(f"{type(error).__name__}: {error}") from error

        logger.info(
            "anthropic | stop_reason=%s | input_tokens=%d output_tokens=%d | request_id=%s",
            response.stop_reason,
            response.usage.input_tokens,
            response.usage.output_tokens,
            getattr(response, "_request_id", None),  # set by the SDK on real HTTP responses
        )
        return ModelTurn(
            stop_reason=response.stop_reason or "unknown",
            text="".join(block.text for block in response.content if isinstance(block, TextBlock)),
            tool_calls=[
                ToolCall(id=block.id, name=block.name, input=dict(block.input))
                for block in response.content
                if isinstance(block, ToolUseBlock)
            ],
            raw_content=list(response.content),
        )


class FakeClient:
    """Deterministic stand-in for the model, for tests and offline integration runs.

    It follows the same protocol as Claude: it asks for `match_canonical_supplier`, then for
    `search_history`, then answers from what the tools returned. Its judgment is deliberately
    simple: a proven supplier gets its historical category and business unit; anything else is
    a new or unconfirmed supplier for review, filed as other / general_admin.
    """

    provider = "fake"
    model = "fake-scripted-v1"

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        answer_schema: dict[str, Any],
    ) -> ModelTurn:
        invoice = json.loads(messages[0]["content"])["invoice"]
        results = tool_results(messages)
        if len(results) == 0:
            return tool_turn("match_canonical_supplier", supplier_arguments(invoice), 1)

        # A tool error (e.g. an invoice with no supplier name or tax ID) reads as "nothing found".
        match = NO_MATCH if "error" in results[0] else SupplierMatch.model_validate(results[0])
        proven = match.proven_match()
        if len(results) == 1:
            if proven is None:
                return tool_turn("search_history", supplier_arguments(invoice), 2)
            return tool_turn("search_history", {"canonical_supplier": proven.canonical_supplier}, 2)

        history = NO_HISTORY if "error" in results[1] else HistorySearch.model_validate(results[1])
        text = json.dumps(fake_answer(invoice, match, history))
        return ModelTurn("end_turn", text, [], [{"type": "text", "text": text}])


NO_MATCH = SupplierMatch(supplier_name=None, supplier_tax_id=None, candidates=[])
NO_HISTORY = HistorySearch(
    match_count=0,
    canonical_suppliers={},
    categories={},
    business_units={},
    review_decisions={},
    recent_rows=[],
)


def supplier_arguments(invoice: dict[str, Any]) -> dict[str, Any]:
    """The invoice's supplier name and, if it has one, its tax ID, as tool arguments."""
    arguments = {"supplier_name": invoice["supplier_name"] or ""}
    if invoice["supplier_tax_id"]:
        arguments["supplier_tax_id"] = invoice["supplier_tax_id"]
    return arguments


def tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The tool outputs sent back so far, oldest first, decoded from JSON."""
    results: list[dict[str, Any]] = []
    for message in messages[1:]:
        if message["role"] == "user" and isinstance(message["content"], list):
            results.extend(json.loads(block["content"]) for block in message["content"])
    return results


def tool_turn(name: str, arguments: dict[str, Any], call_number: int) -> ModelTurn:
    call_id = f"fake_call_{call_number}"
    block = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return ModelTurn("tool_use", "", [ToolCall(call_id, name, arguments)], [block])


def fake_answer(
    invoice: dict[str, Any], match: SupplierMatch, history: HistorySearch
) -> dict[str, Any]:
    proven = match.proven_match()
    if proven is None:
        return {
            "canonical_supplier": invoice["supplier_name_key"] or "UNKNOWN SUPPLIER",
            "predicted_spend_category": "other",
            "predicted_business_unit": "general_admin",
            "review_decision": "needs_review",
            "confidence": 0.4,
            "decision_explanation": (
                f"No known supplier is proven for {invoice['supplier_name']!r} (match result: "
                f"{match.summary()}), so it is treated as new or unconfirmed. Category and "
                "business unit need a person to confirm them."
            ),
        }

    category = next(iter(history.categories), proven.typical_category)
    business_unit = next(iter(history.business_units), proven.typical_business_unit)
    return {
        "canonical_supplier": proven.canonical_supplier,
        "predicted_spend_category": category,
        "predicted_business_unit": business_unit,
        "review_decision": "auto_approve",
        "confidence": 0.9 if proven.match_type == "tax_id" else 0.85,
        "decision_explanation": (
            f"Matched to {proven.canonical_supplier} by {proven.match_type} "
            f"({proven.matched_on}). {history.match_count} earlier invoices from this supplier "
            f"were classified as {category} for {business_unit}."
        ),
    }


def build_llm_client(settings: Settings) -> LLMClient:
    """The client chosen by LLM_PROVIDER. There is no silent fallback to the fake."""
    if settings.llm_provider == "fake":
        return FakeClient()
    if settings.llm_provider == "anthropic":
        if settings.anthropic_api_key is None:
            raise ConfigurationError(
                "LLM_PROVIDER=anthropic needs ANTHROPIC_API_KEY (see .env.example)."
            )
        return AnthropicClient(
            api_key=settings.anthropic_api_key.get_secret_value(),
            model=settings.anthropic_model,
            effort=settings.anthropic_effort,
        )
    raise ConfigurationError("Set LLM_PROVIDER to 'fake' or 'anthropic' (see .env.example).")
