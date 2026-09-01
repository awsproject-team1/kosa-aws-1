"""Bedrock model candidates used for role-specific, measured evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """A named Bedrock Runtime model candidate."""

    label: str
    model_id: str


# Defaults are the highest-ranked quality-gate candidates from the 45-model, five-run
# comparison captured at docs/evaluations/data/bedrock-model-evaluation-20260831.md.
ROLE_CANDIDATES: Final[dict[str, tuple[ModelCandidate, ...]]] = {
    "parent": (
        ModelCandidate("Gemma 3 4B IT", "google.gemma-3-4b-it"),
        ModelCandidate("GLM 4.7 Flash", "zai.glm-4.7-flash"),
        ModelCandidate("Mistral Large 3", "mistral.mistral-large-3-675b-instruct"),
        ModelCandidate("Qwen3 32B", "qwen.qwen3-32b-v1:0"),
        ModelCandidate("Qwen3-Coder-30B-A3B", "qwen.qwen3-coder-30b-a3b-v1:0"),
    ),
    "policy_qa": (
        ModelCandidate("Voxtral Mini 3B 2507", "mistral.voxtral-mini-3b-2507"),
        ModelCandidate("Qwen3-Coder-30B-A3B", "qwen.qwen3-coder-30b-a3b-v1:0"),
        ModelCandidate("GLM 4.7 Flash", "zai.glm-4.7-flash"),
        ModelCandidate("Qwen3 32B", "qwen.qwen3-32b-v1:0"),
        ModelCandidate("Ministral 14B 3.0", "mistral.ministral-3-14b-instruct"),
    ),
    "assessment": (
        ModelCandidate("Nova Micro", "amazon.nova-micro-v1:0"),
        ModelCandidate("Qwen3-Coder-30B-A3B", "qwen.qwen3-coder-30b-a3b-v1:0"),
        ModelCandidate("Gemma 3 4B IT", "google.gemma-3-4b-it"),
        ModelCandidate("Nova Lite", "amazon.nova-lite-v1:0"),
        ModelCandidate("Nova Pro", "amazon.nova-pro-v1:0"),
    ),
    "remediation_deployment": (ModelCandidate("Devstral 2 123B", "mistral.devstral-2-123b"),),
}

DEFAULT_REGION: Final[str] = "us-east-1"
DEFAULT_RUNS: Final[int] = 5
ROLE_NAMES: Final[dict[str, str]] = {
    "parent": "Parent",
    "policy_qa": "POLICY_QA",
    "assessment": "ASSESSMENT",
    "remediation_deployment": "REMEDIATION_DEPLOYMENT",
}
