# Security

LedgerRoute handles invoice payloads, supplier identifiers, historical context, and model decision traces. Treat those records and their backups as sensitive company data.

## Reporting a vulnerability

Use the repository's private vulnerability reporting feature when available. Otherwise, contact the maintainer through the contact details on their GitHub profile before sharing exploit details. Public issues should contain only a high-level description without credentials, private invoices, or actionable exploit material.

Include the affected version or commit, a synthetic reproduction, the impact, and any suggested mitigation. Fixes currently target the latest code on the default branch; there is no long-term support policy for older versions.

## Deployment boundary

The current API has no built-in authentication, authorization, tenant isolation, or rate limiting. Deploy behind an authenticated gateway on a private network. Set request limits and strict JSON validation, restrict documentation endpoints as needed, and protect database volumes and logs.

Invoice text is untrusted input to the model. Tools are limited to supplier matching and historical lookup, and code enforces review policies, but those checks do not guarantee that every model classification is correct or immune to adversarial invoice content. Integrating systems must enforce their own approval and payment controls.

Using the Anthropic provider sends normalized invoice fields and lookup context to that provider. Keep secrets in deployment configuration, keep company data outside Git, and define access and retention controls for payloads, tool traces, logs, and exports. See [deployment guidance](docs/deployment.md) for the full operational boundary.
