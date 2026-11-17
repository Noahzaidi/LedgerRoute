# System design

LedgerRoute separates data cleaning, business inference, policy enforcement, and analytical export. The HTTP layer delegates to plain Python functions so the same pipeline can run in tests and integrations.

## Processing contract

1. **Ingest:** Pydantic validates the payload shape. `document_id` is unique; duplicates are rejected without modifying the stored invoice.
2. **Normalize:** pure functions parse dates and amounts, standardize names and tax IDs, and emit quality flags. Unreadable values become null rather than being guessed.
3. **Resolve:** the agent receives the normalized invoice, quality flags, catalogs, review rules, and access to two internal lookup tools.
4. **Enforce policies:** code independently gathers evidence and checks the model's answer. The stored record contains both the proposal and the final decision.
5. **Export:** successful resolution records are copied into a separate analytical database. Export completion is recorded only after the analytical commit succeeds.

Ingestion status is `normalized`, `resolved`, or `failed`. A `resolved` invoice can still have the business decision `needs_review` and can be exported in that state. Downstream consumers must apply their own review gate.

## Normalization

| Input | Behavior |
| --- | --- |
| Dates | ISO, slash-separated ISO, and supported day-first formats become ISO dates. Dates with a valid alternate month-first reading are flagged `ambiguous_date`. |
| Monetary values | Locale-aware strings and numbers become `Decimal`; unreadable values are flagged. A difference greater than 0.01 between base plus tax and total emits `amount_mismatch`. |
| Supplier names | Collapse whitespace; derive an uppercase, accent-insensitive matching key. |
| Tax IDs | Remove spaces, dots, and dashes; uppercase the result. Country prefixes are retained and required for an exact match when present in the catalog. |
| Currency and country | Uppercase and validate the code shape. This does not verify membership in a complete ISO registry. |
| Missing fields | Preserve null and emit `missing:<field>`. |
| OCR confidence | Values below 0.60 emit `low_confidence:<field>` when the field is present. |

Quality flags describe input issues. The business review rules below do not block on every quality flag; consumers needing a strict accounting validation gate must add that policy to their integration.

## Agent and evidence

`app/agent.py` runs a bounded loop of at most six model turns. On tool requests, the backend executes each tool and returns the results together. Assistant content, including provider-specific thinking blocks, is preserved when continuing the conversation. The final text must validate against a JSON schema and Pydantic; category and business-unit values are limited to the configured catalogs.

`match_canonical_supplier` returns up to three suppliers ranked by match type: tax ID, exact canonical name or alias, then fuzzy name similarity. Exact tax ID has score 1.0, alias 0.95, and fuzzy matches are reported from 0.80. Similarity is supporting evidence, not proof of identity. One exact match proves a supplier only when the tax ID and name evidence do not conflict. An alias contradicted by the supplied tax ID is insufficient.

`search_history` queries historical resolutions by canonical name, tax ID, or raw supplier name. It returns aggregate category, business-unit, and review-decision counts across matching rows, plus the ten most recent rows. New model resolutions do not enter this history automatically, which prevents unreviewed outputs from reinforcing themselves.

`app/llm.py` defines a small provider interface. The Anthropic adapter uses the official SDK with structured output, a 120-second request timeout, and two SDK retries. The offline client follows the same tool protocol with deterministic answers for local integration. It is a test aid, not a substitute for evaluating model performance on reviewed company invoices.

The prompt requests supplier matching and history search. The runtime requires at least one tool call, and policy checks independently gather their own evidence after the model answers.

## Review policies

| Rule | Condition |
| --- | --- |
| `RR1` | The invoice lacks a tax ID and no supplier is proven. |
| `RR2` | OCR confidence is below 0.60 for supplier name or description. |
| `RR3` | Identity evidence conflicts, history is split across categories or business units, or the proposed classification conflicts with the evidence. |
| `RR4` | The supplier is not proven, including new suppliers and fuzzy-only matches. Conflicting exact matches are handled by `RR3`. |
| `LOW_CONFIDENCE` | The model proposes `auto_approve` with confidence below 0.70. |

When a rule fires, the final decision becomes `needs_review`, confidence is capped at 0.60, and the rule and reason are recorded. The model's original proposal remains available for audit and evaluation. Catalog text informs the model; deterministic enforcement is implemented in `app/review_rules.py` and must be updated when policy semantics change.

## Operational storage

`app/storage.py` owns the SQL and opens a separate operational connection per request.

| Table | Purpose |
| --- | --- |
| `invoices` | Raw and normalized input, quality flags, current processing status, timestamps, and export state. |
| `resolutions` | Every attempt, including final result, model answer, applied rules, tool trace, error, provider, and model. The latest attempt is the invoice's current resolution. |
| `history` | Reviewed historical context seeded idempotently by `history_id`. Existing seeded rows are not overwritten. |

The analytical `resolved_invoices` table has one flat row per exported resolution, keyed by `(document_id, resolution_id)`. `latest_resolved_invoices` selects the latest exported resolution for each invoice. Amounts are `REAL` in analytics; exact decimal representations remain in the operational payload. This export is intended for reporting, not as an authoritative accounting ledger.

## Export consistency

The exporter selects resolved invoices with a null `exported_at`, writes their current results to analytics, commits, and then marks the operational invoices as exported. Repeating an export without new results writes no rows. Re-resolution resets the export flag and produces another analytical version.

If execution stops between commits, the next run rewrites the same analytical keys without duplicates. The two databases do not share a transaction. Concurrent resolution and export can race when updating export state; an integrating scheduler must serialize those operations in the current release. An outbox with version-aware acknowledgement is the planned path for concurrent workloads.

A failed re-resolution leaves earlier analytical rows intact. The analytical latest view can therefore show an older successful result while the operational invoice is currently failed. Reconcile downstream workflows using both resolution identity and processing status.

## Failure handling

Provider errors, refusals, truncated or invalid answers, answers without tools, and the turn limit result in `failed`. The service stores the error and partial tool trace without inventing business fields. Calling resolve again creates a new attempt; it is not an idempotent retry endpoint. Failed attempts are excluded from export.

Resolution is synchronous. Request time includes provider calls and retries, so integrations need suitable timeouts and must inspect invoice state before retrying after a network disconnect. There is no queue, lease, tenant boundary, invoice correction endpoint, or human review endpoint in this release.
