"""Constrained Bedrock Converse adapter that generates a minimal Terraform patch.

This is the C Remediation Agent (ADR-0018, BEDROCK_MODEL_SELECTION.md): given a confirmed
Finding and its immutable IaC Snapshot, it produces the smallest repository-scoped
Terraform change that resolves the violation. It performs no AWS write and no apply; it
returns a `RemediationPatch` bound to the snapshot's commit/customer/repository, and the
patch bytes are content-addressed so an identical change always yields the same artifact.

Boundary, mirroring the Assessment evaluator:
- The model may choose only the changed files and their new contents. Identity
  (finding_id, base_commit_sha, customer/repository) is reconstructed from the
  authoritative context, never from model output.
- Paths are validated as repository-relative (the `RemediationPatch` contract rejects
  absolute paths and `..`), so the model cannot propose a write outside the repository.
- The action gate (TERRAFORM_PATCH only) is enforced by the worker before this runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Protocol

from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    ModelProfile,
    ModelProfileRole,
    RemediationContext,
    RemediationDecision,
    RemediationPatch,
)


class BedrockPatchError(ValueError):
    """Raised when a model response is not a safe structured Terraform patch."""


class BedrockConverseClient(Protocol):
    """Minimal provider boundary; the Lambda runtime supplies the regional client."""

    def converse(self, **kwargs: object) -> Mapping[str, object]: ...


class BedrockPatchGenerator:
    """Generate one snapshot-bound Terraform patch for a TERRAFORM_PATCH decision.

    Injected as the C Worker's `PatchAction` (replacing the fail-closed
    `UnavailablePatchAction`). The Worker validates that the returned patch is bound to
    the requested finding and snapshot before persisting it.
    """

    def __init__(self, *, client: BedrockConverseClient, model_profile: ModelProfile) -> None:
        if client is None:
            raise TypeError("client is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        if model_profile.role is not ModelProfileRole.REMEDIATION:
            raise BedrockPatchError("model profile is not approved for remediation")
        self._client = client
        self._model_profile = model_profile

    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        snapshot = context.snapshot
        finding = context.finding

        response = self._client.converse(
            modelId=self._model_profile.model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": self._request_body(context)}]}],
            inferenceConfig={"temperature": 0, "maxTokens": 4096},
        )
        changes = _response_changes(_response_object(response))

        # Content-addressed digest over the normalized change set bound to the base
        # commit. Same finding + commit + changes -> same artifact identity, so an
        # at-least-once retry produces an identical patch.
        digest = _content_digest(
            finding_id=finding.finding_id,
            commit_sha=snapshot.commit_sha,
            changes=changes,
        )
        changed_paths = tuple(sorted(changes))
        try:
            return RemediationPatch(
                finding_id=finding.finding_id,
                base_commit_sha=snapshot.commit_sha,
                artifact=ArtifactReference(
                    artifact_id=(
                        f"remediation-patch:{snapshot.repository_id}:{finding.finding_id}:{digest}"
                    ),
                    artifact_type=ArtifactType.REMEDIATION_PATCH,
                    content_sha256=digest,
                    customer_id=snapshot.customer_id,
                    repository_id=snapshot.repository_id,
                ),
                changed_paths=changed_paths,
            )
        except (TypeError, ValueError) as error:
            # The model proposed an unsafe path (absolute or `..`) or an empty change set.
            raise BedrockPatchError("model patch is outside the repository boundary") from error

    def _request_body(self, context: RemediationContext) -> str:
        finding = context.finding
        snapshot = context.snapshot
        return json.dumps(
            {
                "finding": {
                    "finding_id": finding.finding_id,
                    "resource_id": finding.resource_id,
                    "rule_id": finding.rule_id,
                    "rule_version": finding.rule_version,
                    "perspective": finding.perspective.value,
                    "severity": finding.severity,
                    "rationale": finding.rationale,
                },
                "snapshot": {
                    "repository_id": snapshot.repository_id,
                    "commit_sha": snapshot.commit_sha,
                },
                "evidence_references": list(context.evidence_references),
            },
            separators=(",", ":"),
            sort_keys=True,
        )


_SYSTEM_PROMPT = (
    "Generate the smallest Terraform change that resolves the supplied Finding within "
    "the repository. Return one JSON object only, with exactly changes: a non-empty "
    "object mapping each repository-relative file path to its complete new file "
    "contents. Change only what the Finding requires and touch as few files as possible. "
    "Every path must be repository-relative (no leading slash and no '..' segment). Do "
    "not create resources unrelated to the Finding, do not perform any AWS write or "
    "apply, and do not wrap the JSON in code fences or add prose."
)


def _content_digest(*, finding_id: str, commit_sha: str, changes: Mapping[str, str]) -> str:
    payload = json.dumps(
        {
            "finding_id": finding_id,
            "base_commit_sha": commit_sha,
            "changes": {path: changes[path] for path in sorted(changes)},
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _response_object(response: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(response, Mapping):
        raise BedrockPatchError("Bedrock response is invalid")
    output = response.get("output")
    if not isinstance(output, Mapping):
        raise BedrockPatchError("Bedrock response output is missing")
    message = output.get("message")
    if not isinstance(message, Mapping):
        raise BedrockPatchError("Bedrock response message is missing")
    content = message.get("content")
    if not isinstance(content, list) or len(content) != 1 or not isinstance(content[0], Mapping):
        raise BedrockPatchError("Bedrock response must contain one text block")
    text = content[0].get("text")
    if not isinstance(text, str):
        raise BedrockPatchError("Bedrock response text is missing")
    try:
        value = json.loads(_strip_json_fence(text))
    except json.JSONDecodeError as error:
        raise BedrockPatchError("Bedrock response is not JSON") from error
    if not isinstance(value, dict) or set(value) != {"changes"}:
        raise BedrockPatchError("Bedrock response fields are invalid")
    return value


def _response_changes(value: Mapping[str, object]) -> dict[str, str]:
    changes = value.get("changes")
    if not isinstance(changes, Mapping) or not changes:
        raise BedrockPatchError("changes must be a non-empty object")
    result: dict[str, str] = {}
    for path, contents in changes.items():
        if not isinstance(path, str) or not path.strip():
            raise BedrockPatchError("change path must be a non-empty string")
        if not isinstance(contents, str) or not contents:
            raise BedrockPatchError("change contents must be a non-empty string")
        result[path] = contents
    return result


def _strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()
