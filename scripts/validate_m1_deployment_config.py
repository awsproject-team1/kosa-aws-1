#!/usr/bin/env python3
"""Fail closed on inconsistent protected M1 deployment configuration.

The workflow passes only references and selector metadata to this process. Error
messages intentionally identify fields, never protected values.

Run it as a module so the repository root is on the import path — this gate reads the
resource-type allow-list from `agent.runtime`, which is the single source for it:

    python -m scripts.validate_m1_deployment_config
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping
from pathlib import Path

from agent.runtime.actual_resource_tool_factory import ACTUAL_READ_RESOURCE_TYPES
from agent.runtime.github_tool import require_github_repository_full_name

MODEL_PROFILE_PATH = Path(__file__).parents[1] / "fixtures" / "m1" / "assessment_model_profile.json"
COMMON_TARGET_FIELDS = frozenset(
    {
        "customer_id",
        "repository_id",
        "policy_profile_id",
        "commit_sha",
        "github_repository",
        "github_token_secret_id",
        "aws_account_id",
        "aws_read_role_arn",
        "aws_external_id_secret_id",
    }
)
#: A target declares the evaluated resources one of two ways and never both: the legacy
#: single bucket, or an explicit `(resource_type, resource_id)` list. Accepting both at once
#: would leave two answers to "what may this deployment evaluate?".
LEGACY_RESOURCE_FIELD = "s3_bucket_id"
RESOURCE_LIST_FIELD = "resources"
RESOURCE_FIELDS = frozenset({"resource_type", "resource_id"})
#: Resource types the Worker has an Actual read adapter for. Imported, not restated: a
#: hand-copied list would let this gate accept a type the Worker cannot read.
SUPPORTED_RESOURCE_TYPES = frozenset(ACTUAL_READ_RESOURCE_TYPES)
SCOPE_FIELDS = frozenset({"repository_id", "policy_profile_id"})
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
ACCOUNT_ID = re.compile(r"^[0-9]{12}$")


class DeploymentConfigurationError(ValueError):
    """Protected deployment metadata is missing, malformed, or inconsistent."""


def validate_environment(environment: Mapping[str, str]) -> str:
    mode = _required(environment.get("M1_ASSESSMENT_MODE"), "M1_ASSESSMENT_MODE")
    if mode not in {"fixture", "live"}:
        raise DeploymentConfigurationError("M1_ASSESSMENT_MODE must be fixture or live")

    expected_account = _required(
        environment.get("EXPECTED_AWS_ACCOUNT_ID"), "EXPECTED_AWS_ACCOUNT_ID"
    )
    if ACCOUNT_ID.fullmatch(expected_account) is None:
        raise DeploymentConfigurationError("EXPECTED_AWS_ACCOUNT_ID must be 12 digits")
    region = _required(environment.get("AWS_REGION"), "AWS_REGION")
    if mode == "live" and region != _approved_model_profile_region():
        raise DeploymentConfigurationError(
            "AWS_REGION must match the approved M1 Model Profile region"
        )
    scope = _scope(environment.get("ASSESSMENT_SCOPE_JSON"))

    runtime_raw = environment.get("M1_ASSESSMENT_RUNTIME_JSON", "")
    secret_arns_raw = environment.get("M1_ASSESSMENT_SECRET_ARNS", "")
    read_role_arns_raw = environment.get("M1_ASSESSMENT_READ_ROLE_ARNS", "")
    protected_values = (runtime_raw, secret_arns_raw, read_role_arns_raw)
    if not all(isinstance(value, str) for value in protected_values):
        raise DeploymentConfigurationError("M1 protected values must be strings")

    if mode == "fixture":
        if any(protected_values):
            raise DeploymentConfigurationError(
                "fixture mode requires all M1 protected values to be empty"
            )
        return mode

    if not all(protected_values):
        missing = (
            name
            for name, value in (
                ("M1_ASSESSMENT_RUNTIME_JSON", runtime_raw),
                ("M1_ASSESSMENT_SECRET_ARNS", secret_arns_raw),
                ("M1_ASSESSMENT_READ_ROLE_ARNS", read_role_arns_raw),
            )
            if not value
        )
        raise DeploymentConfigurationError(
            "live mode requires protected value: " + ", ".join(missing)
        )

    targets = _targets(runtime_raw)
    target_scope = {
        (target["customer_id"], target["repository_id"], target["policy_profile_id"])
        for target in targets
    }
    if target_scope != scope:
        raise DeploymentConfigurationError(
            "assessment scope and M1 runtime selector sets must match exactly"
        )

    configured_secret_arns = _csv(secret_arns_raw, "M1_ASSESSMENT_SECRET_ARNS")
    configured_read_roles = _csv(read_role_arns_raw, "M1_ASSESSMENT_READ_ROLE_ARNS")
    github_secret_arns: set[str] = set()
    external_id_secret_arns: set[str] = set()
    target_read_roles: set[str] = set()
    for target in targets:
        if COMMIT_SHA.fullmatch(target["commit_sha"]) is None:
            raise DeploymentConfigurationError(
                "commit_sha must be a lowercase 40-character Git SHA"
            )
        if ACCOUNT_ID.fullmatch(target["aws_account_id"]) is None:
            raise DeploymentConfigurationError("aws_account_id must be 12 digits")
        if target["aws_account_id"] != expected_account:
            raise DeploymentConfigurationError(
                "runtime target account must match EXPECTED_AWS_ACCOUNT_ID"
            )
        if not _secret_arn(target["github_token_secret_id"], region, expected_account):
            raise DeploymentConfigurationError(
                "github_token_secret_id must be an approved Secrets Manager ARN"
            )
        if not _secret_arn(target["aws_external_id_secret_id"], region, expected_account):
            raise DeploymentConfigurationError(
                "aws_external_id_secret_id must be an approved Secrets Manager ARN"
            )
        if not _role_arn(target["aws_read_role_arn"], expected_account):
            raise DeploymentConfigurationError("aws_read_role_arn must be an approved IAM role ARN")
        if not _repository_name(target["github_repository"]):
            raise DeploymentConfigurationError(
                "github_repository must be a canonical owner/repository name"
            )
        github_secret_arns.add(target["github_token_secret_id"])
        external_id_secret_arns.add(target["aws_external_id_secret_id"])
        target_read_roles.add(target["aws_read_role_arn"])

    if github_secret_arns & external_id_secret_arns:
        raise DeploymentConfigurationError(
            "GitHub token and AWS External ID secret ARN sets must be disjoint"
        )
    target_secret_arns = github_secret_arns | external_id_secret_arns
    if configured_secret_arns != target_secret_arns:
        raise DeploymentConfigurationError(
            "M1_ASSESSMENT_SECRET_ARNS must match runtime secret references exactly"
        )
    if configured_read_roles != target_read_roles:
        raise DeploymentConfigurationError(
            "M1_ASSESSMENT_READ_ROLE_ARNS must match runtime read roles exactly"
        )
    return mode


def _approved_model_profile_region() -> str:
    try:
        profile = json.loads(MODEL_PROFILE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DeploymentConfigurationError(
            "approved M1 Model Profile fixture is unreadable"
        ) from error
    if not isinstance(profile, Mapping):
        raise DeploymentConfigurationError("approved M1 Model Profile must be an object")
    return _required(profile.get("region"), "approved M1 Model Profile region")


def _scope(raw: object) -> set[tuple[str, str, str]]:
    parsed = _json(raw, "ASSESSMENT_SCOPE_JSON")
    if not isinstance(parsed, dict):
        raise DeploymentConfigurationError("ASSESSMENT_SCOPE_JSON must be an object")
    selectors: set[tuple[str, str, str]] = set()
    count = 0
    for customer_id, entries in parsed.items():
        customer = _required(customer_id, "assessment scope customer_id")
        if not isinstance(entries, list):
            raise DeploymentConfigurationError("assessment scope customer entries must be arrays")
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != SCOPE_FIELDS:
                raise DeploymentConfigurationError("assessment scope selector fields are invalid")
            selector = (
                customer,
                _required(entry.get("repository_id"), "repository_id"),
                _required(entry.get("policy_profile_id"), "policy_profile_id"),
            )
            selectors.add(selector)
            count += 1
    if len(selectors) != count:
        raise DeploymentConfigurationError("assessment scope selectors must be unique")
    return selectors


def _targets(raw: str) -> tuple[dict[str, str], ...]:
    parsed = _json(raw, "M1_ASSESSMENT_RUNTIME_JSON")
    if not isinstance(parsed, list) or not parsed:
        raise DeploymentConfigurationError("M1_ASSESSMENT_RUNTIME_JSON must be a non-empty array")
    targets: list[dict[str, str]] = []
    for value in parsed:
        if not isinstance(value, dict):
            raise DeploymentConfigurationError("M1 runtime target fields are invalid")
        target = {name: _required(value.get(name), name) for name in _target_field_names(value)}
        _validate_resources(value)
        targets.append(target)
    selectors = {
        (target["customer_id"], target["repository_id"], target["policy_profile_id"])
        for target in targets
    }
    if len(selectors) != len(targets):
        raise DeploymentConfigurationError("M1 runtime selectors must be unique")
    return tuple(targets)


def _target_field_names(value: Mapping[str, object]) -> frozenset[str]:
    """Return the string fields to require, refusing a target that declares resources twice."""
    provided = set(value)
    if provided == COMMON_TARGET_FIELDS | {LEGACY_RESOURCE_FIELD}:
        return COMMON_TARGET_FIELDS | {LEGACY_RESOURCE_FIELD}
    if provided == COMMON_TARGET_FIELDS | {RESOURCE_LIST_FIELD}:
        return COMMON_TARGET_FIELDS
    raise DeploymentConfigurationError("M1 runtime target fields are invalid")


def _validate_resources(value: Mapping[str, object]) -> None:
    """Validate the explicit resource list, if the target uses one."""
    if RESOURCE_LIST_FIELD not in value:
        return
    resources = value[RESOURCE_LIST_FIELD]
    if not isinstance(resources, list) or not resources:
        raise DeploymentConfigurationError("M1 runtime target resources must be a non-empty array")
    coordinates: set[tuple[str, str]] = set()
    for resource in resources:
        if not isinstance(resource, dict) or set(resource) != RESOURCE_FIELDS:
            raise DeploymentConfigurationError("M1 runtime resource fields are invalid")
        resource_type = _required(resource.get("resource_type"), "resource_type")
        resource_id = _required(resource.get("resource_id"), "resource_id")
        if resource_type not in SUPPORTED_RESOURCE_TYPES:
            raise DeploymentConfigurationError(
                "M1 runtime resource_type has no Actual read adapter"
            )
        coordinates.add((resource_type, resource_id))
    if len(coordinates) != len(resources):
        raise DeploymentConfigurationError("M1 runtime resources must be unique")


def _json(raw: object, field_name: str) -> object:
    if not isinstance(raw, str) or not raw.strip():
        raise DeploymentConfigurationError(f"{field_name} is required")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as error:
        raise DeploymentConfigurationError(f"{field_name} must be valid JSON") from error


def _csv(raw: str, field_name: str) -> set[str]:
    values = raw.split(",")
    if any(not value or value != value.strip() for value in values) or len(set(values)) != len(
        values
    ):
        raise DeploymentConfigurationError(
            f"{field_name} must contain canonical unique non-empty values"
        )
    return set(values)


def _required(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DeploymentConfigurationError(f"{field_name} must be a non-empty string")
    if value != value.strip():
        raise DeploymentConfigurationError(f"{field_name} must not contain surrounding whitespace")
    return value


def _secret_arn(value: str, region: str, account_id: str) -> bool:
    return (
        re.fullmatch(rf"arn:[^:]+:secretsmanager:{re.escape(region)}:{account_id}:secret:.+", value)
        is not None
    )


def _role_arn(value: str, account_id: str) -> bool:
    return re.fullmatch(rf"arn:[^:]+:iam::{account_id}:role/.+", value) is not None


def _repository_name(value: str) -> bool:
    try:
        require_github_repository_full_name(value)
    except ValueError:
        return False
    return True


def main() -> int:
    try:
        mode = validate_environment(os.environ)
    except DeploymentConfigurationError as error:
        print(f"M1 deployment configuration invalid: {error}", file=sys.stderr)
        return 2
    print(f"assessment deployment configuration validated for {mode} mode")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
