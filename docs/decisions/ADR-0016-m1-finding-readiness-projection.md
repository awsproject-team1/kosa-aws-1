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

**정정 2026-09-05 (ADR-0024 §2).** Readiness averages the status contribution
(`STATUS_SCORES`: PASS 100, FAIL 0), not the result's `score` field, and only over
judged coordinates. `INSUFFICIENT_EVIDENCE` and `MANUAL_REVIEW` are reported as
`undetermined_evaluations` instead of entering the mean as 0 — "could not check" is
not "violated". A plan with no judged coordinate has no Readiness.

`DRIFT` results are excluded from Readiness. Drift states whether the IaC and the
AWS Actual perspective agree, which is not a degree of compliance; including its
binary alignment value would raise the representative score for a resource whose
IaC and Actual are consistently unsafe. Drift still reaches the user as its own
results and Findings, and it is what M2 remediation reads to decide between
patching the IaC and syncing the current commit.

Because Coverage counts `Resource × Rule × Perspective`, an Assessment that
evaluates more than one perspective must not let the first task decide the
immutable denominator. `AssessmentResourceWork.planned_coordinates` carries the
server-fixed plan when it cannot be derived from a single resolved Rule set.
ADR-0020 §5 later replaced the count this field once carried with the
`(resource_id, rule_id, perspective)` set, so the denominator and the comparison
boundary read the same plan.

## Consequences

M2 receives stable customer-scoped Finding IDs without reinterpreting raw AI
output. The initial report can safely display no representative score while work
is incomplete. The current report projection is calculated from immutable items;
a future counter/materialization migration must preserve this formula and never
publish a partial score as final.
