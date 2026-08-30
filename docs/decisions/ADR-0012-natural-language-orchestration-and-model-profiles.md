# ADR-0012: Natural-language orchestration and role-specific model profiles

## Context

Users can start a known action through an explicit UI/API control or express an intent in
natural language. Treating both entries identically makes explicit actions unnecessarily
probabilistic, while allowing an LLM to create jobs or decide authorization would weaken the
customer boundary. Policy Q&A, compliance assessment, Terraform remediation, and natural-
language routing also have different quality, latency, and cost characteristics.

## Decision

Explicit UI/API actions enter their matching LangGraph Subgraph directly. Natural-language
requests enter the Parent Orchestrator Agent, which handles Policy Q&A directly or determines
the candidate intent and selectors before proposing `ASSESSMENT`, `REMEDIATION`, or
`DEPLOYMENT`. The Parent has no authority to create a Job, validate scope, approve a deployment,
or change AWS. The Backend validates the proposed selectors against the JWT and requires user
confirmation before an Assessment, Remediation, or Deployment starts.

Parent (including Policy Q&A), Assessment, Remediation, and Deployment use separate approved
Model Profiles. Each profile pins the model and relevant prompt/rubric versions, and is chosen
from Golden Dataset and repeated-evaluation results for that role. Changes require the existing
quality gate and result/audit recording of the active profile.

## Consequences

The Parent is an LLM Agent for natural-language routing and Policy Q&A, not a replacement for
the Backend's authorization or job-dispatch responsibilities. Explicit Assessment, Remediation,
and Deployment workflow entry remains deterministic. Workflow implementations must add versioned
runtime Contract fields for active Model Profile persistence before model routing is deployed.
Producer and consumer owners review those Contract, fixture, and Golden Dataset changes together.
