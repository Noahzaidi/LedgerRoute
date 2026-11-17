# Contributing to LedgerRoute

Use issues to describe a concrete integration need or a reproducible bug. Include the expected behavior and a minimal synthetic input. Keep company invoices, credentials, customer identifiers, and private logs out of public issues and pull requests.

## Local setup

Follow the [README](README.md#quick-start), install the `dev` extra, and use Python 3.12 or newer. Run these commands with the virtual environment's Python:

```bash
python -m pytest
python -m ruff format --check app tests scripts
python -m ruff check app tests scripts
python -m mypy app
python scripts/run_sample.py
```

Tests must run without provider credentials or live network calls. Use temporary databases and deterministic or scripted model clients. Provider protocol changes should be verified against the installed SDK and covered at the adapter boundary.

## Change guidelines

- Keep deterministic validation and policy enforcement outside the model.
- Preserve raw inputs and prior resolution attempts when changing storage behavior.
- Never turn uncertain evidence into a proven supplier match without a documented rule.
- Add focused regression coverage for behavior changes and update the relevant API or deployment documentation.
- Keep synthetic fixtures separate from company data. Explain fixture changes that affect expected decisions.

Pull requests should describe the concrete behavior change, the reason for it, the checks performed, and any compatibility or operational impact. Security-sensitive reports belong in the process described in [SECURITY.md](SECURITY.md).
