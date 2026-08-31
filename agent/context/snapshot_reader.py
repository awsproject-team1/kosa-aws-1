"""D(Remediation/Deployment)를 위한 Assessment 입력 수집.

이 모듈은 read-only D tool 경계 두 개(GitHub Integration Tool과 AWS Resource Tool)를
소비해, 그 read 결과를 AI 평가 경계(C)가 그대로 사용하는 하나의 불변 Assessment 입력
번들로 결합한다. ADR-0007에 따라 두 기반 tool은 모두 read-only이며 승인된 resource로
scope가 제한된다. 이 collector는 어떤 write 표면도 추가하지 않는다. 요청은 승인된
``customer_id`` 하나를 명시하고, 두 축(IaC repository와 AWS account)이 이를 구조적으로
공유하므로 collector는 customer 경계를 넘어 read할 수 없다.

collector는 아무것도 평가하지 않는다. C가 필요로 하는 IaC snapshot(IAC 관점)과 AWS
Actual resource view(AWS_ACTUAL 관점)를 수집하기만 한다. Drift 판정
(IAC vs AWS_ACTUAL)은 별도 경계다.
"""

from dataclasses import dataclass

from agent.runtime.aws_resource_tool import AwsResourceTool, AwsResourceView
from agent.runtime.github_tool import GitHubTool, IaCSnapshotRequest
from packages.contracts import (
    AwsResourceOperation,
    AwsResourceQuery,
    IaCSnapshot,
)


class AssessmentInputError(RuntimeError):
    """Assessment 입력 번들 수집의 기본 실패 타입."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AwsResourceSelector:
    """단일 account scope 안에서 AWS Resource Tool에 대한 하나의 read.

    selector는 ``AwsResourceQuery``와 동일하게 operation과 대상을 명시하되, scope
    필드(customer_id/aws_account_id)는 제외한다. 그 필드는 요청이 한 번만 제공하여
    모든 AWS read가 하나의 account를 공유하게 한다.
    """

    operation: AwsResourceOperation
    resource_type: str
    resource_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation, AwsResourceOperation):
            raise TypeError("operation must be an AwsResourceOperation")
        if not isinstance(self.resource_type, str) or not self.resource_type.strip():
            raise ValueError("resource_type must be a non-empty string")
        if self.resource_id is not None and (
            not isinstance(self.resource_id, str) or not self.resource_id.strip()
        ):
            raise ValueError("resource_id must be a non-empty string when provided")

    def to_query(self, *, customer_id: str, aws_account_id: str) -> AwsResourceQuery:
        """이 selector에 대해 scope가 적용된 ``AwsResourceQuery``를 만든다.

        Contract 자체 검증(예: READ_RESOURCE는 resource_id 필수)이 여기에 적용되므로,
        잘못된 형태의 selector는 query 생성 시점에 실패한다.
        """
        return AwsResourceQuery(
            customer_id=customer_id,
            aws_account_id=aws_account_id,
            operation=self.operation,
            resource_type=self.resource_type,
            resource_id=self.resource_id,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class SnapshotReadRequest:
    """하나의 Assessment 입력 번들을 수집하기 위한 불변 요청.

    승인된 IaC 좌표(customer_id, repository_id, commit_sha)와 승인된 AWS account,
    그리고 read할 resource selector를 명시한다. 두 축은 같은 ``customer_id``를
    명시해야 하며, 이는 단일 값 구조로 보장되므로 scope 밖 요청이 tool에 도달하지 않는다.
    """

    customer_id: str
    repository_id: str
    commit_sha: str
    aws_account_id: str
    aws_selectors: tuple[AwsResourceSelector, ...]

    def __post_init__(self) -> None:
        for name in ("customer_id", "repository_id", "commit_sha", "aws_account_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.aws_selectors, tuple):
            raise TypeError("aws_selectors must be a tuple")
        if not self.aws_selectors:
            raise ValueError("aws_selectors must not be empty")
        for selector in self.aws_selectors:
            if not isinstance(selector, AwsResourceSelector):
                raise TypeError("aws_selectors must contain AwsResourceSelector items")

    def iac_request(self) -> IaCSnapshotRequest:
        return IaCSnapshotRequest(
            customer_id=self.customer_id,
            repository_id=self.repository_id,
            commit_sha=self.commit_sha,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AssessmentInputBundle:
    """AI 평가 경계를 위한 불변 read-only Assessment 입력.

    승인된 하나의 customer scope에 대한 IaC snapshot(IAC 관점)과 AWS Actual resource
    view(AWS_ACTUAL 관점)를 담는다. 번들은 어떤 mutation 표면도 노출하지 않으며,
    snapshot과 view 모두 이미 frozen된 Contract/runtime 값이다.
    """

    customer_id: str
    iac_snapshot: IaCSnapshot
    aws_resources: tuple[AwsResourceView, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.customer_id, str) or not self.customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(self.iac_snapshot, IaCSnapshot):
            raise TypeError("iac_snapshot must be an IaCSnapshot")
        if not isinstance(self.aws_resources, tuple):
            raise TypeError("aws_resources must be a tuple")
        for view in self.aws_resources:
            if not isinstance(view, AwsResourceView):
                raise TypeError("aws_resources must contain AwsResourceView items")

    def to_dict(self) -> dict[str, object]:
        return {
            "customer_id": self.customer_id,
            "iac_snapshot": self.iac_snapshot.to_dict(),
            "aws_resources": [view.to_dict() for view in self.aws_resources],
        }


class AssessmentInputCollector:
    """read-only IaC와 AWS read를 하나의 Assessment 입력 번들로 결합한다."""

    def __init__(self, *, github_tool: GitHubTool, aws_tool: AwsResourceTool) -> None:
        self._github_tool = github_tool
        self._aws_tool = aws_tool

    def collect(self, request: SnapshotReadRequest) -> AssessmentInputBundle:
        """하나의 customer scope에 대한 IaC snapshot과 AWS Actual view를 read한다.

        어느 tool에서 발생하든 scope 오류와 not-found 오류는 그대로 전파되므로,
        호출자는 tool이 내는 동일한 read-only 경계 실패를 보게 된다. 두 read는
        하나의 불변 번들로 모인다.
        """
        if not isinstance(request, SnapshotReadRequest):
            raise TypeError("request must be a SnapshotReadRequest")

        iac_snapshot = self._github_tool.read_iac_snapshot(request.iac_request())

        views: list[AwsResourceView] = []
        for selector in request.aws_selectors:
            query = selector.to_query(
                customer_id=request.customer_id,
                aws_account_id=request.aws_account_id,
            )
            if selector.operation is AwsResourceOperation.READ_RESOURCE:
                views.append(self._aws_tool.read_resource(query))
            else:
                views.extend(self._aws_tool.list_resources(query))

        return AssessmentInputBundle(
            customer_id=request.customer_id,
            iac_snapshot=iac_snapshot,
            aws_resources=tuple(views),
        )
