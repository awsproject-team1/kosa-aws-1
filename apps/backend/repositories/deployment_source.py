"""A-owned reader assembling the stored facts a deployment is created from (ADR-0019 §4).

`POST /remediations/{id}/deployments`는 네 전제조건을 fail-closed로 확인한 뒤에만 Deployment를
만든다: 저장된 decision이 actionable인가, C Worker 결과가 있는가, `TERRAFORM_PATCH`의 대상 commit이
default branch에서 도달 가능한가, JWT customer scope가 맞는가. 앞의 둘은 `REMEDIATION#{id}` item
하나에 함께 있으므로 단일 strongly-consistent get으로 읽는다. 셋째만 GitHub read가 필요하고, 그건
D 소유 경계이므로 `DeploymentCommitResolver` port로 주입받는다.

대상 commit은 action마다 다르다(ADR-0019 §3):
- `ACTUAL_SYNC`: `RemediationSyncTarget.commit_sha`. 이미 `IAC` 관점을 통과한 현재 default branch
  commit이므로 도달 가능성이 그 값의 정의에 포함된다.
- `TERRAFORM_PATCH`: 사람이 PR을 merge한 **default branch의 merge commit**. patch의
  `base_commit_sha`가 아니다 — base는 patch를 만든 시점의 스냅샷이고, 그걸 apply하면 사람이
  승인한 코드가 아닌 것을 배포하게 된다.
"""

from collections.abc import Mapping

from apps.backend.api.deployments import DeploymentSource
from apps.backend.deployment import DeploymentCommitResolver
from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    RemediationAction,
    RemediationPatch,
)


class RemediationNotFoundError(LookupError):
    """The remediation a deployment was requested for does not exist."""


class DynamoDbDeploymentSourceReader:
    """Read one remediation's deployable facts, resolving the target commit."""

    def __init__(self, table: DynamoTable, *, commits: DeploymentCommitResolver) -> None:
        if table is None:
            raise TypeError("table is required")
        if commits is None:
            raise TypeError("commits resolver is required")
        self._table = table
        self._commits = commits

    def get_deployment_source(self, *, customer_id: str, remediation_id: str) -> DeploymentSource:
        for value, name in ((customer_id, "customer_id"), (remediation_id, "remediation_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"REMEDIATION#{remediation_id}"},
                # A deployment is created from this read, so a stale replica must not
                # be able to show a decision without the worker result that follows it.
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("remediation read failed") from None
        if item is None:
            raise RemediationNotFoundError("remediation not found")
        return self._source_from_item(_mapping(item), customer_id, remediation_id)

    def _source_from_item(
        self, item: Mapping[str, object], customer_id: str, remediation_id: str
    ) -> DeploymentSource:
        if item.get("entity_type") != "REMEDIATION" or item.get("customer_id") != customer_id:
            raise StoredDataError("stored remediation is outside the customer scope")
        try:
            context = _mapping(item.get("context"))
            snapshot = _mapping(context.get("snapshot"))
            decision = _mapping(item.get("decision"))
            action = RemediationAction(decision.get("action"))
            repository_id = _string(snapshot.get("repository_id"), "repository_id")
            # Verification is bound to the exact before-state Assessment. Without it a
            # later comparison would have to guess which Assessment produced the
            # Finding, so its absence closes the path rather than defaulting.
            source_assessment_id = _string(
                context.get("source_assessment_id"), "source_assessment_id"
            )
            result = item.get("result")
        except StoredDataError:
            raise
        except (TypeError, ValueError):
            raise StoredDataError("stored remediation is invalid") from None

        commit_sha, reachable = self._target_commit(
            customer_id=customer_id,
            repository_id=repository_id,
            action=action,
            snapshot_commit_sha=_string(snapshot.get("commit_sha"), "commit_sha"),
            result=result,
        )
        return DeploymentSource(
            remediation_id=remediation_id,
            customer_id=customer_id,
            repository_id=repository_id,
            commit_sha=commit_sha,
            source_assessment_id=source_assessment_id,
            action=action,
            has_worker_result=result is not None,
            commit_reachable_from_default_branch=reachable,
        )

    def _target_commit(
        self,
        *,
        customer_id: str,
        repository_id: str,
        action: RemediationAction,
        snapshot_commit_sha: str,
        result: object,
    ) -> tuple[str, bool]:
        """Return the deployment's target commit and whether it is on the default branch.

        The snapshot commit is the fallback whenever a target cannot be resolved. It is
        never deployed in that case — the caller refuses on `reachable=False` — but
        `DeploymentSource` requires a commit, and reporting the assessed one keeps the
        refusal legible instead of inventing a value.
        """
        if result is None:
            return snapshot_commit_sha, False
        stored = _mapping(result)
        if action is RemediationAction.ACTUAL_SYNC:
            if stored.get("kind") != RemediationAction.ACTUAL_SYNC.value:
                raise StoredDataError("stored result does not match the remediation decision")
            target = _mapping(stored.get("sync_target"))
            # The sync target is by definition the current default-branch commit that
            # already passed the IaC perspective (ADR-0019 §3), so no GitHub read is
            # needed to establish reachability.
            return _string(target.get("commit_sha"), "sync target commit_sha"), True
        if action is not RemediationAction.TERRAFORM_PATCH:
            # A non-actionable decision is refused by the caller; there is no target.
            return snapshot_commit_sha, False
        if stored.get("kind") != RemediationAction.TERRAFORM_PATCH.value:
            raise StoredDataError("stored result does not match the remediation decision")
        patch = _patch(_mapping(stored.get("patch")))
        merged = self._commits.resolve_default_branch_commit(
            customer_id=customer_id, repository_id=repository_id, patch=patch
        )
        if merged is None:
            return patch.base_commit_sha, False
        if not isinstance(merged, str) or not merged.strip():
            raise StoredDataError("resolved default branch commit is invalid")
        return merged, True


def _patch(value: Mapping[str, object]) -> RemediationPatch:
    artifact = _mapping(value.get("artifact"))
    changed_paths = value.get("changed_paths")
    if not isinstance(changed_paths, list):
        raise StoredDataError("stored patch changed_paths is invalid")
    try:
        return RemediationPatch(
            finding_id=_string(value.get("finding_id"), "patch finding_id"),
            base_commit_sha=_string(value.get("base_commit_sha"), "base_commit_sha"),
            artifact=ArtifactReference(
                artifact_id=_string(artifact.get("artifact_id"), "artifact_id"),
                artifact_type=ArtifactType(artifact.get("artifact_type")),
                content_sha256=_string(artifact.get("content_sha256"), "content_sha256"),
                customer_id=_string(artifact.get("customer_id"), "artifact customer_id"),
                repository_id=artifact.get("repository_id"),
            ),
            changed_paths=tuple(_string(path, "changed_paths item") for path in changed_paths),
        )
    except StoredDataError:
        raise
    except (TypeError, ValueError):
        raise StoredDataError("stored patch is invalid") from None


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise StoredDataError("stored remediation value must be a mapping")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise StoredDataError(f"stored remediation {name} is invalid")
    return value
