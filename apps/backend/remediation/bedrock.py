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
- **The model sees the snapshot's Terraform body.** 원본 없이 "파일 전체의 새 내용"을
  요구하면 모델은 보지 못한 파일을 지어낸다 — 기존 설정 보존도, 최소 변경도 검사할
  근거가 없다. 그래서 평가가 읽은 것과 같은 read-only `IaCDocumentReader`로 같은 commit의
  본문을 읽어 prompt에 넣고, 응답은 `terraform_change.validate_terraform_changes()`로 그
  본문에 묶는다: snapshot에 있는 파일만, 실제로 달라진 파일만, 리소스 블록을 지우지 않는
  변경만 통과한다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol

from agent.runtime.github_tool import IaCDocument, IaCDocumentReader, IaCSnapshotRequest
from apps.backend.remediation.patch_content import (
    PatchContentError,
    PatchContentStore,
    encode_patch_content,
    patch_content_digest,
)
from apps.backend.remediation.terraform_change import (
    TerraformChangeError,
    validate_terraform_changes,
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
        iac_documents: IaCDocumentReader | None,
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
        if iac_documents is not None and not isinstance(iac_documents, IaCDocumentReader):
            raise TypeError("iac_documents must implement IaCDocumentReader")
        self._client = client
        self._model_profile = model_profile
        self._content_store = content_store
        # `None`은 "원본을 읽을 통로가 없다"는 값이다. 그 상태에서는 generate()가 fail-closed
        # 한다 — 원본 없이 만든 patch는 검증할 수 없고, 검증할 수 없는 patch를 PR로 올리지 않는다.
        self._iac_documents = iac_documents

    def generate(
        self, *, context: RemediationContext, decision: RemediationDecision
    ) -> RemediationPatch:
        if not isinstance(context, RemediationContext):
            raise TypeError("context must be a RemediationContext")
        if not isinstance(decision, RemediationDecision):
            raise TypeError("decision must be a RemediationDecision")
        snapshot = context.snapshot
        finding = context.finding
        document = self._read_document(context)

        response = self._client.converse(
            modelId=self._model_profile.model_id,
            system=[{"text": _SYSTEM_PROMPT}],
            messages=[
                {"role": "user", "content": [{"text": self._request_body(context, document)}]}
            ],
            inferenceConfig={"temperature": 0, "maxTokens": 4096},
        )
        changes = _response_changes(_response_object(response))
        # 경계 위반(절대 경로, `..`)은 snapshot 대조보다 먼저 그 이름으로 거부한다. 둘 다 거부지만
        # 사유가 다르다 — 전자는 모델이 저장소 밖을 가리킨 것이고 후자는 저장소 안의 다른 파일이다.
        for path in changes:
            if path.startswith("/") or ".." in path.split("/"):
                raise BedrockPatchError("model patch is outside the repository boundary")
        try:
            validate_terraform_changes(document, changes)
        except TerraformChangeError as error:
            raise BedrockPatchError(f"model patch is not bound to the snapshot: {error}") from error

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

    def _read_document(self, context: RemediationContext) -> IaCDocument:
        """Read the Terraform body of the exact commit the Finding was evaluated at."""
        if self._iac_documents is None:
            raise BedrockPatchError("Terraform source reader is not configured for this runtime")
        snapshot = context.snapshot
        document = self._iac_documents.read_iac_document(
            IaCSnapshotRequest(
                customer_id=snapshot.customer_id,
                repository_id=snapshot.repository_id,
                commit_sha=snapshot.commit_sha,
            )
        )
        if not isinstance(document, IaCDocument):
            raise BedrockPatchError("Terraform source reader returned an invalid document")
        if (
            document.customer_id != snapshot.customer_id
            or document.repository_id != snapshot.repository_id
            or document.commit_sha != snapshot.commit_sha
        ):
            # 다른 commit의 본문에 대해 만든 patch는 이 snapshot에 적용되지 않는다.
            raise BedrockPatchError("Terraform source document is outside the snapshot")
        return document

    def _request_body(self, context: RemediationContext, document: IaCDocument) -> str:
        finding = context.finding
        snapshot = context.snapshot
        return json.dumps(
            {
                "terraform_files": [
                    {"path": path, "content": content} for path, content in document.files
                ],
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
    "the repository. terraform_files holds the complete current contents of every "
    "Terraform file at the assessed commit; the Finding's resource_id names the AWS "
    "resource that violates the rule, and rationale says what the evaluator observed. "
    "Return one JSON object only, with exactly changes: a non-empty object mapping each "
    "changed file path (which must be one of the supplied terraform_files paths) to its "
    "complete new file contents. Copy the original file and edit only the attributes the "
    "Finding requires: keep every other resource, block, attribute, comment, and ordering "
    "exactly as supplied, do not add files, do not add resources unrelated to the "
    "Finding, never delete or rename an existing resource block, and only use attributes "
    "that exist for that Terraform resource type. Every path must be repository-relative "
    "(no leading slash and no '..' segment). Do not perform any AWS write or apply, and do "
    "not wrap the JSON in code fences or add prose."
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
