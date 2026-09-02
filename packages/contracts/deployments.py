"""GitHub, read-only AWS, and approved Terraform deployment contracts."""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from packages.contracts._validation import require_non_empty_string


class ArtifactType(StrEnum):
    POLICY_ORIGINAL = "POLICY_ORIGINAL"
    TERRAFORM_SNAPSHOT = "TERRAFORM_SNAPSHOT"
    AWS_SNAPSHOT = "AWS_SNAPSHOT"
    REMEDIATION_PATCH = "REMEDIATION_PATCH"
    TERRAFORM_PLAN = "TERRAFORM_PLAN"
    GOLDEN_DATASET = "GOLDEN_DATASET"


class AwsResourceOperation(StrEnum):
    READ_RESOURCE = "READ_RESOURCE"
    LIST_RESOURCES = "LIST_RESOURCES"


@dataclass(frozen=True, slots=True, kw_only=True)
class ArtifactReference:
    artifact_id: str
    artifact_type: ArtifactType
    content_sha256: str
    customer_id: str
    repository_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("artifact_id", "content_sha256", "customer_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact_type, ArtifactType):
            raise TypeError("artifact_type must be an ArtifactType")
        if self.repository_id is not None:
            require_non_empty_string(self.repository_id, "repository_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type.value,
            "content_sha256": self.content_sha256,
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class IaCSnapshot:
    customer_id: str
    repository_id: str
    commit_sha: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.TERRAFORM_SNAPSHOT:
            raise ValueError("artifact must be a TERRAFORM_SNAPSHOT")
        if self.artifact.customer_id != self.customer_id:
            raise ValueError("artifact customer_id must match snapshot customer_id")
        if self.artifact.repository_id != self.repository_id:
            raise ValueError("artifact repository_id must match snapshot repository_id")

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "repository_id": self.repository_id,
            "commit_sha": self.commit_sha,
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationPatch:
    finding_id: str
    base_commit_sha: str
    artifact: ArtifactReference
    changed_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in ("finding_id", "base_commit_sha"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.REMEDIATION_PATCH:
            raise ValueError("artifact must be a REMEDIATION_PATCH")
        if not self.changed_paths:
            raise ValueError("changed_paths must not be empty")
        for path in self.changed_paths:
            require_non_empty_string(path, "changed_paths item")
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("changed_paths items must be repository-relative paths")

    def to_dict(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "base_commit_sha": self.base_commit_sha,
            "artifact": self.artifact.to_dict(),
            "changed_paths": list(self.changed_paths),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceQuery:
    customer_id: str
    aws_account_id: str
    operation: AwsResourceOperation
    resource_type: str
    resource_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("customer_id", "aws_account_id", "resource_type"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.operation, AwsResourceOperation):
            raise TypeError("operation must be an AwsResourceOperation")
        if self.resource_id is not None:
            require_non_empty_string(self.resource_id, "resource_id")
        if self.operation is AwsResourceOperation.READ_RESOURCE and self.resource_id is None:
            raise ValueError("READ_RESOURCE requires resource_id")

    def to_dict(self) -> dict[str, str | None]:
        return {
            "customer_id": self.customer_id,
            "aws_account_id": self.aws_account_id,
            "operation": self.operation.value,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class TerraformPlan:
    deployment_id: str
    commit_sha: str
    plan_hash: str
    artifact: ArtifactReference

    def __post_init__(self) -> None:
        for name in ("deployment_id", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.artifact, ArtifactReference):
            raise TypeError("artifact must be an ArtifactReference")
        if self.artifact.artifact_type is not ArtifactType.TERRAFORM_PLAN:
            raise ValueError("artifact must be a TERRAFORM_PLAN")
        if self.plan_hash != self.artifact.content_sha256:
            raise ValueError("plan_hash must match the Terraform plan artifact digest")

    def to_dict(self) -> dict[str, object]:
        return {
            "deployment_id": self.deployment_id,
            "commit_sha": self.commit_sha,
            "plan_hash": self.plan_hash,
            "artifact": self.artifact.to_dict(),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentApproval:
    deployment_id: str
    approved_by: str
    commit_sha: str
    plan_hash: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "approved_by", "commit_sha", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)

    def matches(self, plan: TerraformPlan) -> bool:
        """Return whether an approval is bound to this exact deployment plan."""
        if not isinstance(plan, TerraformPlan):
            raise TypeError("plan must be a TerraformPlan")
        return (
            self.deployment_id == plan.deployment_id
            and self.commit_sha == plan.commit_sha
            and self.plan_hash == plan.plan_hash
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "approved_by": self.approved_by,
            "commit_sha": self.commit_sha,
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ApplyRunReference:
    """`ApplyDispatchPort.dispatch_apply`가 반환하는 dispatch된 apply run 참조.

    승인된 approval 하나로 dispatch된 GitHub Actions run을 가리킨다. 같은 approval로
    두 번 dispatch돼도 같은 run을 가리켜야 하므로(ADR-0019 §5), 이 값은 dispatch 자체가
    아니라 dispatch된 run의 좌표만 서술한다. 실제 실행/apply 표면은 담지 않는다.
    """

    deployment_id: str
    repository_id: str
    run_id: str

    def __post_init__(self) -> None:
        for name in ("deployment_id", "repository_id", "run_id"):
            require_non_empty_string(getattr(self, name), name)

    def to_dict(self) -> dict[str, str]:
        return {
            "deployment_id": self.deployment_id,
            "repository_id": self.repository_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class VerifiedRunOutcome:
    """`WorkflowRunReader.read_run`이 반환하는 권위 있는 run 완료 사실.

    EventBridge payload를 신뢰하지 않고 `run_id`로 Actions run을 다시 읽어 만든 값이다
    (ADR-0019 §7). 실패도 값이므로(ADR-0017·0018) 재조회 실패나 mismatch는 예외가 아니라
    `conclusion`으로 표현한다. D Worker는 이 값의 `workflow_path`/`repository_id`/`ref`/
    `plan_hash`를 승인 사실과 대조한 뒤에만 상태를 진행한다.
    """

    run_id: str
    workflow_path: str
    repository_id: str
    ref: str
    conclusion: str
    plan_hash: str

    def __post_init__(self) -> None:
        for name in ("run_id", "workflow_path", "repository_id", "ref", "conclusion", "plan_hash"):
            require_non_empty_string(getattr(self, name), name)

    @property
    def succeeded(self) -> bool:
        """GitHub Actions 규약대로 성공 결론만 참으로 본다."""
        return self.conclusion == "success"

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "workflow_path": self.workflow_path,
            "repository_id": self.repository_id,
            "ref": self.ref,
            "conclusion": self.conclusion,
            "plan_hash": self.plan_hash,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceSnapshot:
    """`ActualRereadPort.reread_actual`가 반환하는 apply 후 단일 리소스 Actual.

    M1 read-only AWS Resource Tool 재사용이며 write 표면이 없다(ADR-0007). `attributes`는
    서술적 read 상태일 뿐 리소스를 변경할 수 있는 handle/token을 담지 않는다. Contract가
    앱 모듈의 freeze 유틸에 의존하지 않도록 값은 문자열 매핑으로 제한하고 최상위를
    read-only로 감싼다.
    """

    customer_id: str
    aws_account_id: str
    resource_type: str
    resource_id: str
    attributes: Mapping[str, str]

    def __post_init__(self) -> None:
        for name in ("customer_id", "aws_account_id", "resource_type", "resource_id"):
            require_non_empty_string(getattr(self, name), name)
        if not isinstance(self.attributes, Mapping):
            raise TypeError("attributes must be a mapping")
        for key, value in self.attributes.items():
            require_non_empty_string(key, "attributes key")
            if not isinstance(value, str):
                raise TypeError("attributes values must be strings")
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "aws_account_id": self.aws_account_id,
            "resource_type": self.resource_type,
            "resource_id": self.resource_id,
            "attributes": dict(self.attributes),
        }
