# Roadmap

The implemented foundation covers structured invoice ingestion, deterministic normalization, evidence-based supplier resolution, policy enforcement, trace persistence, and incremental analytical export. The next stages focus on integrating those capabilities into managed finance operations. Items below are planned work rather than available features.

## Review and data stewardship

- Add a review queue and correction endpoint with reviewer identity, timestamps, and a change trail.
- Promote approved corrections into trusted historical context through an explicit stewardship step.
- Version reference catalogs and policies, and record the version used by each resolution.
- Support corrected invoice versions while retaining stable document identity and prior inputs.

## Reliable orchestration

- Move resolution to background jobs with leases, bounded retries, and job status endpoints.
- Add request idempotency for resolution attempts and batch processing.
- Replace invoice export flags with a transactional outbox and version-aware acknowledgement.
- Introduce PostgreSQL and schema migrations for concurrent operational workloads.

## Organization controls

- Provide application-level authentication, authorization, and tenant boundaries.
- Add structured logs, metrics, readiness checks, and per-resolution token and cost accounting.
- Make request limits, retention policies, and field-level data handling configurable.
- Add reviewed deployment examples and recovery verification for supported environments.

## Evaluation and integrations

- Maintain a labeled evaluation set independent of historical lookup data.
- Measure supplier resolution, classification quality, and review routing against human-reviewed outcomes.
- Add CSV ingestion and warehouse or Parquet export adapters.
- Add calibrated decision thresholds and policy controls for amount and date validation.
