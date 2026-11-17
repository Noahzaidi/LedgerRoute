import json
import sqlite3
from typing import Any

import pytest

from app.agent import MAX_TURNS, build_answer_schema, build_system_prompt, run_agent
from app.llm import FakeClient, LLMError, ModelTurn, ToolCall
from app.normalization import normalize_invoice
from app.reference import ReferenceData
from app.schemas import AgentAnswer, InvoiceIn, NormalizedInvoice
from app.tools import TOOL_DEFINITIONS

UBER_ANSWER = {
    "canonical_supplier": "UBER BV",
    "predicted_spend_category": "travel",
    "predicted_business_unit": "sales",
    "review_decision": "auto_approve",
    "confidence": 0.9,
    "decision_explanation": "Matched to UBER BV by alias.",
}


class ScriptedClient:
    """Replays prepared model turns (or raises prepared errors) and records what it received."""

    provider = "scripted"
    model = "scripted"

    def __init__(self, turns: list[ModelTurn | Exception]) -> None:
        self.turns = list(turns)
        self.received: list[list[dict[str, Any]]] = []

    def complete(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        answer_schema: dict[str, Any],
    ) -> ModelTurn:
        self.received.append(list(messages))
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def tool_turn(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> ModelTurn:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": arguments}
    return ModelTurn("tool_use", "", [ToolCall(call_id, name, arguments)], [block])


def answer_turn(answer: dict[str, Any]) -> ModelTurn:
    text = json.dumps(answer)
    return ModelTurn("end_turn", text, [], [{"type": "text", "text": text}])


MATCH_UBER = tool_turn("match_canonical_supplier", {"supplier_name": "Uber"})


@pytest.fixture
def uber(seed_by_id: dict[str, dict[str, Any]]) -> tuple[NormalizedInvoice, list[str]]:
    return normalize_invoice(InvoiceIn.model_validate(seed_by_id["doc_new_009"]))


def test_fake_client_calls_both_tools_then_answers(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    run = run_agent(*uber, FakeClient(), history_db, reference)

    assert run.error is None
    assert run.answer is not None
    assert (
        run.answer.canonical_supplier,
        run.answer.predicted_spend_category,
        run.answer.predicted_business_unit,
        run.answer.review_decision,
    ) == ("UBER BV", "travel", "sales", "auto_approve")
    assert [step.tool for step in run.trace] == ["match_canonical_supplier", "search_history"]
    assert run.trace[0].summary == "best UBER BV (alias 0.95), 1 candidate(s)"
    assert run.trace[1].input == {"canonical_supplier": "UBER BV"}
    assert run.turns == 3


def test_tool_results_go_back_after_the_assistant_turn(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    client = ScriptedClient([MATCH_UBER, answer_turn(UBER_ANSWER)])

    run_agent(*uber, client, history_db, reference)

    second_request = client.received[1]
    assert [message["role"] for message in second_request] == ["user", "assistant", "user"]
    assert second_request[1]["content"] == MATCH_UBER.raw_content
    result_block = second_request[2]["content"][0]
    assert (result_block["type"], result_block["tool_use_id"], result_block["is_error"]) == (
        "tool_result",
        "call_1",
        False,
    )
    assert json.loads(result_block["content"])["candidates"][0]["canonical_supplier"] == "UBER BV"


def test_answer_without_a_tool_call_fails(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    run = run_agent(*uber, ScriptedClient([answer_turn(UBER_ANSWER)]), history_db, reference)

    assert run.answer is None
    assert run.error == "Model answered without calling a tool."


@pytest.mark.parametrize("stop_reason", ["refusal", "max_tokens"])
def test_unexpected_stop_reason_fails_and_keeps_the_partial_trace(
    stop_reason: str,
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    client = ScriptedClient([MATCH_UBER, ModelTurn(stop_reason, "", [], [])])

    run = run_agent(*uber, client, history_db, reference)

    assert run.answer is None
    assert run.error == f"Model stopped with stop_reason={stop_reason}."
    assert len(run.trace) == 1


def test_llm_error_fails(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    client = ScriptedClient([LLMError("APIConnectionError: Connection error.")])

    run = run_agent(*uber, client, history_db, reference)

    assert run.error == "LLM call failed: APIConnectionError: Connection error."


@pytest.mark.parametrize(
    ("change", "error_start"),
    [
        ({"predicted_spend_category": "food"}, "Invalid final answer: category 'food'"),
        ({"canonical_supplier": "..."}, "Invalid final answer: canonical_supplier has no"),
        ({"canonical_supplier": "́"}, "Invalid final answer: canonical_supplier has no"),
        ({"confidence": 1.5}, "Invalid final answer: 1 validation error"),
    ],
)
def test_invalid_answer_fails(
    change: dict[str, Any],
    error_start: str,
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    client = ScriptedClient([MATCH_UBER, answer_turn({**UBER_ANSWER, **change})])

    run = run_agent(*uber, client, history_db, reference)

    assert run.answer is None
    assert run.error is not None
    assert run.error.startswith(error_start)


def test_run_stops_after_the_turn_limit(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    endless: list[ModelTurn | Exception] = [
        tool_turn("match_canonical_supplier", {"supplier_name": "Uber"}, f"call_{number}")
        for number in range(MAX_TURNS)
    ]

    run = run_agent(*uber, ScriptedClient(endless), history_db, reference)

    assert run.error == f"No answer after {MAX_TURNS} model turns."
    assert len(run.trace) == MAX_TURNS


def test_tool_error_goes_back_to_the_model(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    client = ScriptedClient([tool_turn("delete_invoices", {}), answer_turn(UBER_ANSWER)])

    run = run_agent(*uber, client, history_db, reference)

    assert run.trace[0].is_error
    assert run.trace[0].output == {"error": "Unknown tool: delete_invoices"}
    assert client.received[1][2]["content"][0]["is_error"] is True
    assert run.answer is not None


def test_parallel_tool_calls_go_back_in_one_message(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    calls = [
        ToolCall("a", "match_canonical_supplier", {"supplier_name": "Uber"}),
        ToolCall("b", "search_history", {"canonical_supplier": "UBER BV"}),
    ]
    blocks = [
        {"type": "tool_use", "id": call.id, "name": call.name, "input": call.input}
        for call in calls
    ]
    client = ScriptedClient([ModelTurn("tool_use", "", calls, blocks), answer_turn(UBER_ANSWER)])

    run = run_agent(*uber, client, history_db, reference)

    second_request = client.received[1]
    assert [message["role"] for message in second_request] == ["user", "assistant", "user"]
    assert [block["tool_use_id"] for block in second_request[2]["content"]] == ["a", "b"]
    assert [(step.turn, step.tool) for step in run.trace] == [
        (1, "match_canonical_supplier"),
        (1, "search_history"),
    ]


def test_tools_from_a_truncated_turn_never_run(
    uber: tuple[NormalizedInvoice, list[str]],
    history_db: sqlite3.Connection,
    reference: ReferenceData,
) -> None:
    # A tool call cut off by the token limit: its input may be incomplete.
    truncated = ModelTurn("max_tokens", "", MATCH_UBER.tool_calls, MATCH_UBER.raw_content)

    run = run_agent(*uber, ScriptedClient([truncated]), history_db, reference)

    assert run.trace == []
    assert run.error == "Model stopped with stop_reason=max_tokens."


def test_answer_schema_matches_the_answer_model_and_the_catalogs(
    reference: ReferenceData,
) -> None:
    schema = build_answer_schema(reference)

    properties = schema["properties"]
    assert schema["additionalProperties"] is False  # required by structured outputs
    assert set(schema["required"]) == set(properties) == set(AgentAnswer.model_fields)
    assert properties["predicted_spend_category"]["enum"] == reference.categories
    assert properties["predicted_business_unit"]["enum"] == reference.business_units
    assert properties["review_decision"]["enum"] == ["auto_approve", "needs_review"]
    unsupported = ("minimum", "maximum", "minLength", "maxLength", "pattern")
    assert not any(keyword in json.dumps(schema) for keyword in unsupported)


def test_tool_schemas_are_valid_for_strict_tool_use() -> None:
    for tool in TOOL_DEFINITIONS:
        input_schema = tool["input_schema"]
        assert tool["strict"] is True
        assert input_schema["additionalProperties"] is False
        assert set(input_schema["required"]) <= set(input_schema["properties"])


def test_system_prompt_lists_the_catalogs_and_rules(reference: ReferenceData) -> None:
    prompt = build_system_prompt(reference)

    assert "one of telecom, software, travel" in prompt
    assert "- RR1: If supplier_tax_id is missing" in prompt
    assert "{" not in prompt  # every placeholder was filled
