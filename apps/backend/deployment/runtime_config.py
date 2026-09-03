"""Fail-closed, customer-scoped configuration for the live Deployment Worker path.

M1 assessment runtime과 같은 원리다. 이 JSON은 승인된 `(customer_id, repository_id)` 하나를
apply dispatch·run 재조회·Actual 재조회에 필요한 값들의 **참조**로 매핑한다. secret 값은 Worker만
읽고, 공개 API 호출자는 넘길 수 없다. DeploymentRecord/Job에 없는 `aws_account_id`,
`repository_full_name`, token/credential secret, 재조회 대상 `resource_types`를 여기서 제공한다.

Worker는 세 command에서 서로 다른 표면을 쓰지만(plan/apply/verify) 대상 자체는 하나의 승인된
배포 저장소·계정이므로, 세 값을 한 target에 모아 둔다. resolve 실패는 예외이며(미승인 대상은
work가 실행되지 않는다), 기본값으로 대체하지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent.runtime.github_tool import require_github_repository_full_name


class DeploymentRuntimeConfigurationError(ValueError):
    """Live deployment configuration is absent, malformed, or outside approved scope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeploymentTarget:
    """One server-approved deployment target's runtime references."""

    customer_id: str
    repository_id: str
    repository_full_name: str
    github_token_secret_id: str
    aws_account_id: str
    aws_read_role_arn: str
    aws_external_id_secret_id: str
    resource_types: tuple[str, ...]

    def __post_init__(self) -> None:
        for name in (
            "customer_id",
            "repository_id",
            "repository_full_name",
            "github_token_secret_id",
            "aws_account_id",
            "aws_read_role_arn",
            "aws_external_id_secret_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        require_github_repository_full_name(self.repository_full_name)
        if not isinstance(self.resource_types, tuple) or not self.resource_types:
            raise ValueError("resource_types must be a non-empty tuple")
        for resource_type in self.resource_types:
            if not isinstance(resource_type, str) or not resource_type.strip():
                raise ValueError("resource_types item must be a non-empty string")


class DeploymentRuntimeConfiguration:
    """Resolve one server-approved deployment target without fallback defaults."""

    def __init__(self, targets: tuple[DeploymentTarget, ...]) -> None:
        if not targets or not all(isinstance(target, DeploymentTarget) for target in targets):
            raise ValueError("targets must contain DeploymentTarget values")
        # GitHub token과 AWS external-id secret은 서로 다른 역할이어야 한다(M1과 같은 규칙).
        github_secret_ids = {target.github_token_secret_id for target in targets}
        external_id_secret_ids = {target.aws_external_id_secret_id for target in targets}
        if github_secret_ids & external_id_secret_ids:
            raise ValueError("deployment credential secret roles must be disjoint")
        self._targets = {(target.customer_id, target.repository_id): target for target in targets}
        if len(self._targets) != len(targets):
            raise ValueError("deployment target scope must be unique")

    @classmethod
    def from_json(cls, raw: object) -> DeploymentRuntimeConfiguration:
        if not isinstance(raw, str) or not raw.strip():
            raise DeploymentRuntimeConfigurationError(
                "deployment runtime configuration is required"
            )
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as error:
            raise DeploymentRuntimeConfigurationError(
                "deployment runtime configuration is invalid JSON"
            ) from error
        if not isinstance(values, list):
            raise DeploymentRuntimeConfigurationError(
                "deployment runtime configuration must be a list"
            )
        try:
            return cls(tuple(_target(value) for value in values))
        except (TypeError, ValueError) as error:
            raise DeploymentRuntimeConfigurationError(
                "deployment runtime configuration is invalid"
            ) from error

    @property
    def targets(self) -> tuple[DeploymentTarget, ...]:
        """승인된 target 전체(조립 시 단일 target 구성 확인용)."""
        return tuple(self._targets.values())

    def resolve(self, *, customer_id: str, repository_id: str) -> DeploymentTarget:
        try:
            return self._targets[(customer_id, repository_id)]
        except KeyError:
            raise DeploymentRuntimeConfigurationError(
                "deployment selectors are outside runtime scope"
            ) from None

    def aws_account_id_for(self, customer_id: str, repository_id: str) -> str:
        """`DynamoDbDeploymentWorkRepository`에 주입되는 resolver 어댑터."""
        return self.resolve(customer_id=customer_id, repository_id=repository_id).aws_account_id


def _target(value: object) -> DeploymentTarget:
    if not isinstance(value, Mapping):
        raise TypeError("deployment target must be an object")
    expected = {
        "customer_id",
        "repository_id",
        "repository_full_name",
        "github_token_secret_id",
        "aws_account_id",
        "aws_read_role_arn",
        "aws_external_id_secret_id",
        "resource_types",
    }
    if set(value) != expected:
        raise ValueError("deployment target fields are invalid")
    data = dict(value)
    resource_types = data.get("resource_types")
    if isinstance(resource_types, str) or not isinstance(resource_types, Sequence):
        raise ValueError("resource_types must be a list of strings")
    data["resource_types"] = tuple(resource_types)
    return DeploymentTarget(**data)
