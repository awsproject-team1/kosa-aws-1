"""Fail-closed, customer-scoped configuration for the live M1 worker path.

This deployment JSON maps an approved customer/repository/profile tuple to one
exact Git commit, S3 resource, and secret *references*. Secret values are read
only by the Worker; callers cannot supply them through the public API.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass


class M1RuntimeConfigurationError(ValueError):
    """Live M1 configuration is absent, malformed, or outside approved scope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class M1AssessmentTarget:
    customer_id: str
    repository_id: str
    policy_profile_id: str
    commit_sha: str
    github_repository: str
    github_token_secret_id: str
    aws_account_id: str
    aws_read_role_arn: str
    aws_external_id_secret_id: str
    s3_bucket_id: str

    def __post_init__(self) -> None:
        for name in (
            "customer_id",
            "repository_id",
            "policy_profile_id",
            "commit_sha",
            "github_repository",
            "github_token_secret_id",
            "aws_account_id",
            "aws_read_role_arn",
            "aws_external_id_secret_id",
            "s3_bucket_id",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if re.fullmatch(r"[0-9a-f]{40}", self.commit_sha) is None:
            raise ValueError("commit_sha must be a lowercase 40-character Git SHA")


class M1RuntimeConfiguration:
    """Resolve one server-approved target without fallback defaults."""

    def __init__(self, targets: tuple[M1AssessmentTarget, ...]) -> None:
        if not targets or not all(isinstance(target, M1AssessmentTarget) for target in targets):
            raise ValueError("targets must contain M1AssessmentTarget values")
        github_secret_ids = {target.github_token_secret_id for target in targets}
        external_id_secret_ids = {target.aws_external_id_secret_id for target in targets}
        if github_secret_ids & external_id_secret_ids:
            raise ValueError("M1 credential secret roles must be disjoint")
        self._targets = {
            (target.customer_id, target.repository_id, target.policy_profile_id): target
            for target in targets
        }
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

    def resolve(
        self, *, customer_id: str, repository_id: str, policy_profile_id: str
    ) -> M1AssessmentTarget:
        try:
            return self._targets[(customer_id, repository_id, policy_profile_id)]
        except KeyError:
            raise M1RuntimeConfigurationError(
                "assessment selectors are outside M1 runtime scope"
            ) from None


def _target(value: object) -> M1AssessmentTarget:
    if not isinstance(value, Mapping):
        raise TypeError("M1 assessment target must be an object")
    expected = {
        "customer_id",
        "repository_id",
        "policy_profile_id",
        "commit_sha",
        "github_repository",
        "github_token_secret_id",
        "aws_account_id",
        "aws_read_role_arn",
        "aws_external_id_secret_id",
        "s3_bucket_id",
    }
    if set(value) != expected:
        raise ValueError("M1 assessment target fields are invalid")
    return M1AssessmentTarget(**dict(value))
