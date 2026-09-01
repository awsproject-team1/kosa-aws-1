# ADR-0016: C derives M1 Findings and Readiness from immutable evaluation results

## Context

M1 requires Finding, Evidence, Readiness Score, and Coverage, while M2 remediation
requires a Finding ID. The existing C worker already creates the authoritative,
validated `EvaluationResult` for each Resource × Rule × Perspective, but no role
had owned the two Assessment-level projections.

## Decision

C owns both outputs. A result with `FAIL`, `MANUAL_REVIEW`, or
`INSUFFICIENT_EVIDENCE` produces one immutable Finding whose deterministic ID is
derived from the result identity. Finding persistence uses the customer-scoped
Assessment prefix and is idempotent with at-least-once worker delivery.

C calculates Readiness only after the immutable evaluation plan has complete
Coverage. It uses the evaluator's 0–100 score weighted by severity
`LOW=1`, `MEDIUM=2`, `HIGH=4`, `CRITICAL=8`; `OUT_OF_SCOPE` does not affect the
score. Readiness remains unavailable for incomplete coverage or execution errors.
Coverage remains a separate mechanical execution-rate indicator.

## Consequences

M2 receives stable customer-scoped Finding IDs without reinterpreting raw AI
output. The initial report can safely display no representative score while work
is incomplete. The current report projection is calculated from immutable items;
a future counter/materialization migration must preserve this formula and never
publish a partial score as final.
