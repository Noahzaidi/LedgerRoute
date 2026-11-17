"""Run LedgerRoute's synthetic invoice pipeline with isolated temporary databases.

The default provider is offline, regardless of .env. Pass --provider anthropic explicitly
to use billed model calls with credentials from .env or the environment.

    python scripts/run_sample.py
    python scripts/run_sample.py --provider anthropic
"""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from fastapi.testclient import TestClient

from app.config import PROJECT_ROOT, Settings
from app.main import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["fake", "anthropic"], default="fake")
    args = parser.parse_args()
    with TemporaryDirectory(prefix="ledgerroute-") as directory:
        run_sample(
            Settings(
                llm_provider=args.provider,
                data_dir=PROJECT_ROOT / "data",
                operational_db_path=Path(directory) / "operational.db",
                analytics_db_path=Path(directory) / "analytics.db",
            )
        )


def run_sample(settings: Settings) -> None:
    seed_path = settings.data_dir / "new_post_ocr_inputs.json"
    seed = json.loads(seed_path.read_text(encoding="utf-8"))

    with TestClient(create_app(settings)) as client:
        health = client.get("/health").json()
        print(f"\nLedgerRoute | provider: {health['llm_provider']} ({health['llm_model']})")

        imported = client.post("/invoices/import", json=seed).json()
        if len(imported["created"]) != len(seed) or imported["invalid"] or imported["duplicates"]:
            raise RuntimeError(f"Unexpected import result: {imported}")
        print(f"Imported {len(imported['created'])} invoices from {seed_path.name}\n")

        invoices = [client.post(f"/invoices/{item['document_id']}/resolve").json() for item in seed]
        print_summary(invoices)
        if any(invoice["status"] != "resolved" for invoice in invoices):
            raise RuntimeError("One or more sample invoices failed to resolve.")

        first_export = client.post("/exports/run").json()
        second_export = client.post("/exports/run").json()
        print(
            f"\nExport: {first_export['exported']} rows on the first run, "
            f"{second_export['exported']} on the second (nothing new to export)."
        )
        if first_export["exported"] != len(seed) or second_export["exported"] != 0:
            raise RuntimeError("Incremental export did not produce the expected row counts.")

        print_trace(next(i for i in invoices if i["document_id"] == "doc_new_010"))
    print("\nSample completed. Temporary databases are removed on exit.")


def print_summary(invoices: list[dict[str, Any]]) -> None:
    header = f"{'document':<12} {'canonical supplier':<34} {'category':<22} {'unit':<14}"
    print(f"{header} {'decision':<13} {'conf':>4}  rules")
    for invoice in invoices:
        resolution = invoice["resolution"]
        result = resolution["result"]
        if result is None:
            print(f"{invoice['document_id']:<12} FAILED: {resolution['error']}")
            continue
        rules = ", ".join(rule["rule_id"] for rule in resolution["applied_rules"]) or "-"
        print(
            f"{invoice['document_id']:<12} {result['canonical_supplier']:<34} "
            f"{result['predicted_spend_category']:<22} {result['predicted_business_unit']:<14} "
            f"{result['review_decision']:<13} {result['confidence']:>4.2f}  {rules}"
        )


def print_trace(invoice: dict[str, Any]) -> None:
    resolution = invoice["resolution"]
    print(f"\nTrace of {invoice['document_id']} ({invoice['normalized']['supplier_name']}):")
    for step in resolution["trace"]:
        arguments = json.dumps(step["input"], ensure_ascii=False)
        print(f"  turn {step['turn']}: {step['tool']}({arguments})")
        print(f"          -> {step['summary']}")
    if resolution["result"] is not None:
        print(f"  model said:  {resolution['model_answer']['review_decision']}")
        print(f"  final:       {resolution['result']['review_decision']}")
        print(f"  explanation: {resolution['result']['decision_explanation']}")


if __name__ == "__main__":
    main()
