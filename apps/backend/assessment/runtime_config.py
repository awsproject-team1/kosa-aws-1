"""Fail-closed, customer-scoped configuration for the live M1 worker path.

This deployment JSON maps an approved customer/repository pair to one Git revision — a
pinned `commit_sha`, or a `branch` whose HEAD is read when each Assessment starts (ADR-0027) —
the AWS resources that may be evaluated, and secret *references*. Secret values are read only by
the Worker; callers cannot supply them through the public API.

**Policy Profile은 여기 없다.** 두 경계는 다른 질문에 답한다.

    Runtime configuration — 이 고객이 어떤 Repository와 AWS Resource를 읽을 수 있는가
    DynamoDB Policy Catalog — 이 고객이 어떤 게시된 Policy Profile을 쓸 수 있는가

Profile을 배포 JSON key에 넣으면, 고객이 정책을 승인·게시할 때마다 인프라 배포가 필요해진다 —
"업로드한 정책이 승인 직후 평가에 쓰인다"는 목표와 정면으로 충돌한다. 사용 가능한 Profile은
Catalog가 정하고, 어떤 판본을 쓸지는 Assessment 생성 시점에 고정한다.

The evaluated resources are an approved list, not a customer-supplied selector. An
Assessment may name which of them it is about, but it can never introduce a resource the
deployment was not configured for.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass

from agent.runtime.github_tool import (
    require_git_branch_name,
    require_github_repository_full_name,
)
from apps.backend.assessment.actual import SUPPORTED_ACTUAL_RESOURCE_TYPES

#: The legacy single-S3 field. A deployment configured before the resource expansion keeps
#: working: the bucket becomes the one approved `AWS::S3::Bucket` resource.
_LEGACY_S3_FIELD = "s3_bucket_id"
_S3_RESOURCE_TYPE = "AWS::S3::Bucket"


class M1RuntimeConfigurationError(ValueError):
    """Live M1 configuration is absent, malformed, or outside approved scope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class M1AssessmentResource:
    """One AWS resource this deployment is approved to read and evaluate."""

    resource_type: str
    resource_id: str

    def __post_init__(self) -> None:
        for name in ("resource_type", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.resource_type not in SUPPORTED_ACTUAL_RESOURCE_TYPES:
            # Configuring a type with no read adapter would produce an Assessment that
            # cannot read its own subject, which is not the same as a compliant resource.
            raise ValueError(f"resource_type {self.resource_type!r} has no Actual read adapter")


@dataclass(frozen=True, slots=True, kw_only=True)
class M1AssessmentTarget:
    customer_id: str
    repository_id: str
    #: 정확히 하나만 설정한다. `commit_sha`는 평가 대상 commit을 배포 시점에 고정하고, `branch`는
    #: Assessment를 시작할 때마다 그 branch의 HEAD를 읽는다(ADR-0027). 고정 commit은 코드가
    #: 고쳐져 main이 나아가도 옛 commit을 계속 읽어 같은 FAIL을 반복했다(2026-09-05 sandbox).
    commit_sha: str | None = None
    branch: str | None = None
    github_repository: str
    github_token_secret_id: str
    aws_account_id: str
    aws_read_role_arn: str
    aws_external_id_secret_id: str
    resources: tuple[M1AssessmentResource, ...]

    def __post_init__(self) -> None:
        for name in (
            "customer_id",
            "repository_id",
            "github_repository",
            "github_token_secret_id",
            "aws_account_id",
            "aws_read_role_arn",
            "aws_external_id_secret_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if (self.commit_sha is None) == (self.branch is None):
            raise ValueError("exactly one of commit_sha or branch must be set")
        if self.commit_sha is not None and (
            not isinstance(self.commit_sha, str)
            or re.fullmatch(r"[0-9a-f]{40}", self.commit_sha) is None
        ):
            raise ValueError("commit_sha must be a lowercase 40-character Git SHA")
        if self.branch is not None:
            require_git_branch_name(self.branch)
        require_github_repository_full_name(self.github_repository)
        if not self.resources or not all(
            isinstance(resource, M1AssessmentResource) for resource in self.resources
        ):
            raise ValueError("resources must contain M1AssessmentResource values")
        coordinates = {
            (resource.resource_type, resource.resource_id) for resource in self.resources
        }
        if len(coordinates) != len(self.resources):
            raise ValueError("approved resources must be unique")

    @property
    def resource_types(self) -> tuple[str, ...]:
        """The resource types this target needs read adapters for, without duplicates."""
        ordered: list[str] = []
        for resource in self.resources:
            if resource.resource_type not in ordered:
                ordered.append(resource.resource_type)
        return tuple(ordered)

    def resolve_resource(
        self, selector: M1AssessmentResource | None = None
    ) -> M1AssessmentResource:
        """Return the approved resource an Assessment names, or the only one if it names none.

        A selector that is outside the approved list is refused rather than read. The
        Assessment record is server-written, but it is not the approval boundary — this
        configuration is, and it is the only place the two coordinates are cross-checked.
        """
        if selector is None:
            if len(self.resources) != 1:
                raise M1RuntimeConfigurationError(
                    "assessment must name the evaluated resource when several are approved"
                )
            return self.resources[0]
        if not isinstance(selector, M1AssessmentResource):
            raise TypeError("selector must be an M1AssessmentResource or None")
        if selector not in self.resources:
            raise M1RuntimeConfigurationError("assessment resource is outside M1 runtime scope")
        return selector


class M1RuntimeConfiguration:
    """Resolve one server-approved target without fallback defaults."""

    def __init__(self, targets: tuple[M1AssessmentTarget, ...]) -> None:
        if not targets or not all(isinstance(target, M1AssessmentTarget) for target in targets):
            raise ValueError("targets must contain M1AssessmentTarget values")
        github_secret_ids = {target.github_token_secret_id for target in targets}
        external_id_secret_ids = {target.aws_external_id_secret_id for target in targets}
        if github_secret_ids & external_id_secret_ids:
            raise ValueError("M1 credential secret roles must be disjoint")
        self._targets = {(target.customer_id, target.repository_id): target for target in targets}
        if len(self._targets) != len(targets):
            raise ValueError("M1 assessment target scope must be unique")

    @classmethod
    def from_json(cls, raw: object) -> M1RuntimeConfiguration:
        if not isinstance(raw, str) or not raw.strip():
            raise M1RuntimeConfigurationError("M1 runtime configuration is required")
        try:
            values = json.loads(raw)
        except json.JSONDecodeError as error:
            raise M1RuntimeConfigurationError("M1 runtime configuration is invalid JSON") from error
        if not isinstance(values, list):
            raise M1RuntimeConfigurationError("M1 runtime configuration must be a list")
        try:
            return cls(tuple(_target(value) for value in values))
        except (TypeError, ValueError) as error:
            raise M1RuntimeConfigurationError("M1 runtime configuration is invalid") from error

    def resolve(self, *, customer_id: str, repository_id: str) -> M1AssessmentTarget:
        try:
            return self._targets[(customer_id, repository_id)]
        except KeyError:
            raise M1RuntimeConfigurationError(
                "assessment selectors are outside M1 runtime scope"
            ) from None


#: 평가 대상 revision을 정하는 두 필드. 정확히 하나만 온다 — 둘 다면 "어느 commit을 읽는가"에
#: 답이 두 개고, 둘 다 없으면 답이 없다.
_REVISION_FIELDS = frozenset({"commit_sha", "branch"})
_COMMON_FIELDS = frozenset(
    {
        "customer_id",
        "repository_id",
        "github_repository",
        "github_token_secret_id",
        "aws_account_id",
        "aws_read_role_arn",
        "aws_external_id_secret_id",
    }
)


def _target(value: object) -> M1AssessmentTarget:
    if not isinstance(value, Mapping):
        raise TypeError("M1 assessment target must be an object")
    provided = set(value)
    revision = provided & _REVISION_FIELDS
    if len(revision) != 1:
        raise ValueError("M1 assessment target must declare exactly one of commit_sha or branch")
    rest = provided - revision
    # Exactly one of the two resource declarations, never both: a target that carries the
    # legacy bucket *and* a resource list has two answers to "what may be evaluated?".
    if rest == _COMMON_FIELDS | {_LEGACY_S3_FIELD}:
        resources = (
            M1AssessmentResource(
                resource_type=_S3_RESOURCE_TYPE, resource_id=value[_LEGACY_S3_FIELD]
            ),
        )
    elif rest == _COMMON_FIELDS | {"resources"}:
        resources = tuple(_resource(entry) for entry in _resource_entries(value["resources"]))
    else:
        raise ValueError("M1 assessment target fields are invalid")
    common = {name: value[name] for name in _COMMON_FIELDS | revision}
    return M1AssessmentTarget(**common, resources=resources)


def _resource_entries(value: object) -> list[object]:
    if not isinstance(value, list) or not value:
        raise ValueError("resources must be a non-empty list")
    return value


def _resource(value: object) -> M1AssessmentResource:
    if not isinstance(value, Mapping) or set(value) != {"resource_type", "resource_id"}:
        raise ValueError("M1 assessment resource fields are invalid")
    return M1AssessmentResource(**dict(value))
