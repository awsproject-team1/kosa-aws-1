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

import json
from collections.abc import Mapping
from typing import Protocol

from apps.backend.remediation.patch_content import (
    PatchContentError,
    PatchContentStore,
    encode_patch_content,
    patch_content_digest,
)
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

    def __init__(
        self,
        *,
        client: BedrockConverseClient,
        model_profile: ModelProfile,
        content_store: PatchContentStore,
    ) -> None:
        if client is None:
            raise TypeError("client is required")
        if not isinstance(model_profile, ModelProfile):
            raise TypeError("model_profile must be a ModelProfile")
        if model_profile.role is not ModelProfileRole.REMEDIATION:
            raise BedrockPatchError("model profile is not approved for remediation")
        if content_store is None:
            # digest만 남기고 내용을 버리면 PR write는 만들 것이 없고 digest는 아무것도 가리키지
            # 않는다. 저장소 없이 생성기를 만들 수 없게 한다.
            raise TypeError("content_store is required")
        self._client = client
        self._model_profile = model_profile
        self._content_store = content_store

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

        # Content-addressed digest over the canonical patch bytes bound to the base
        # commit. Same finding + commit + changes -> same bytes -> same artifact identity,
        # so an at-least-once retry produces an identical patch. The bytes themselves are
        # stored below under that digest; the PR writer reads them back from there.
        try:
            content = encode_patch_content(
                finding_id=finding.finding_id,
                base_commit_sha=snapshot.commit_sha,
                changes=changes,
            )
        except PatchContentError as error:
            # An unsafe path (absolute or `..`) is a boundary violation; anything else
            # (size, empty set) is a patch that cannot be stored.
            message = (
                "model patch is outside the repository boundary"
                if "path" in str(error)
                else f"model patch is not storable: {error}"
            )
            raise BedrockPatchError(message) from error
        digest = patch_content_digest(content)
        changed_paths = tuple(sorted(changes))
        try:
            patch = RemediationPatch(
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
        self._content_store.put(patch=patch, content=content)
        return patch

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
