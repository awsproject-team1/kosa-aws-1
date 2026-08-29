# ADR-0003: Continuous scoring with reliability-triggered anchors

## Context

AI scoring must express nuanced compliance evidence without relying on a Code formula, while repeated evaluations need predictable variance.

## Decision

AI Evaluator uses a continuous 0–100 score by default. Golden Dataset evaluation targets PASS/FAIL accuracy, Evidence Reference accuracy, and same-case agreement of at least 90%, with repeated Score variance within ±10 points. If the variance persistently exceeds that threshold, enable the fixed Anchor set `{0, 15, 30, 50, 70, 85, 100}` and define its Rubric meanings before use.

## Consequences

Score policy, model, prompt, rubric, rule, evidence references, Token/Latency, and validation results must be recorded. Model, Prompt, Rubric, Rule, Policy Document, Context Retrieval, or Tool changes require Golden Dataset and repeated-run regression evaluation.
