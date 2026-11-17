# LedgerRoute data guide

This directory contains synthetic invoice records and example reference catalogs for local integration, automated tests, and API exploration. Supplier names illustrate matching scenarios; the records do not represent customer transactions or endorsements.

## Files

| File | Contents | Runtime use |
| --- | --- | --- |
| `new_post_ocr_inputs.json` | 15 unresolved post-OCR invoices | Import through `/invoices/import` or the sample runner. |
| `historical_resolutions.json` | 51 historical resolution fixtures | Loaded idempotently into the operational history table at startup. |
| `reference_data.json` | 10 canonical suppliers, aliases, category and business-unit catalogs, review rules, and country notes | Loaded into memory at startup. |
| `new_post_ocr_inputs.csv` | Tabular copy of the input fixtures | Inspection in spreadsheet tools. |
| `historical_resolutions.csv` | Tabular copy of historical fixtures | Inspection in spreadsheet tools. |

The HTTP import endpoint accepts JSON. CSV files are provided for inspection; CSV ingestion is a roadmap item.

## Invoice contract

Only `document_id` is required. Use a stable identifier containing letters, numbers, underscores, dots, or hyphens; it is also used in API paths and as the ingestion idempotency key.

```json
{
  "document_id": "invoice_2026_001",
  "supplier_name": "Orange Espana",
  "supplier_tax_id": null,
  "invoice_number": "INV-2026-001",
  "invoice_date": "02/03/2026",
  "currency": "EUR",
  "base_amount": "82,64",
  "tax_amount": "17,35",
  "total_amount": "99,99",
  "description": "Mobile and internet services",
  "country": "ES",
  "field_confidence": {
    "supplier_name": 0.95,
    "description": 0.92
  }
}
```

Amounts can be strings or JSON numbers. Dates accept ISO and supported day-first formats. The service retains unreadable values in the raw payload and surfaces normalization problems as quality flags. OCR confidence values must be between 0 and 1. Extra input fields are retained in the raw payload. The optional `raw_ocr_text` field stores upstream extraction text.

The fixtures cover aliases, OCR substitutions such as `Micr0soft` and `StapIes`, absent tax IDs, new suppliers, and ambiguous descriptions. They support repeatable integration checks; they do not establish classification accuracy on company data.

## Organization configuration

Set `DATA_DIR` to a directory containing your own `reference_data.json` and `historical_resolutions.json` with the same schemas. An empty historical dataset can be represented by `[]`. Maintain the closed catalogs and supplier aliases through a controlled review process, and restart the service after changing reference data.

History rows are seeded by `history_id` with insert-if-missing semantics. Changing an existing row in the JSON file does not replace the stored row. Use a controlled database maintenance process for historical corrections. New model resolutions are kept separately and do not automatically become trusted history.

Use a new operational database when moving from the synthetic data to organization data: changing `DATA_DIR` does not remove previously seeded records. Keep company invoices, reference catalogs, databases, and credentials outside the public repository.
