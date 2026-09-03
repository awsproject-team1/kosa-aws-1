"""Deterministic `terraform show -json` projection shared by A, C, and D.

`plan_hash` is the SHA-256 of an allow-list projection of the plan's
`resource_changes[]`, not of the raw `show -json` output and not of the saved
binary plan (ADR-0019 §1). D produces the value, A re-checks it at approval, C
binds it to readiness, and D re-verifies it immediately before apply. Because
all four call the *same* projection here, the value cannot drift between roles.

The projection is defined as an allow-list, not an exclude-list: a new Terraform
or provider output field must never silently enter the hash and break
reproducibility. `has_destructive_changes` and `mapped_resource_ids` are derived
from the same projected changes, so both readiness gates read exactly what was
hashed rather than a second view of the plan.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

# The eleven allow-listed fields of each `resource_changes[]` entry (ADR-0019 §1) are the six
# below plus the five `_CHANGE_FIELDS` inside its nested `change` object. Any other field (for
# example `timestamp`, `format_version`, `terraform_version`, `prior_state`) is dropped by
# omission, not by an exclude rule.
_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "address",
    "mode",
    "type",
    "name",
    "index",
    "provider_name",
)
_CHANGE_FIELDS: tuple[str, ...] = (
    "actions",
    "before",
    "after",
    "after_unknown",
    "replace_paths",
)


class PlanProjectionError(ValueError):
    """Raised when a `show -json` document cannot be projected deterministically."""


def project_plan_changes(show_json: Mapping[str, object]) -> list[dict[str, object]]:
    """Return the allow-listed, `address`-sorted `resource_changes` projection.

    Only the eleven allow-listed fields survive. The result is a plain structure
    ready for canonical serialization; it is intentionally not hashed here so the
    same projection can feed both `compute_plan_hash` and `has_destructive_changes`.
    """
    if not isinstance(show_json, Mapping):
        raise PlanProjectionError("show_json must be a mapping")
    # A missing or non-list `resource_changes` is a corrupt plan, not an empty one.
    # Defaulting it to `[]` would let a truncated `show -json` project cleanly and
    # then read as non-destructive, silently bypassing the destructive-change gate.
    if "resource_changes" not in show_json:
        raise PlanProjectionError("show_json must carry a resource_changes list")
    resource_changes = show_json["resource_changes"]
    if not isinstance(resource_changes, Sequence) or isinstance(resource_changes, str | bytes):
        raise PlanProjectionError("resource_changes must be a list")

    projected: list[dict[str, object]] = []
    for entry in resource_changes:
        if not isinstance(entry, Mapping):
            raise PlanProjectionError("each resource change must be a mapping")
        # `change` and `change.actions` must be present: `actions` is the sole
        # basis of `has_destructive_changes`, so a missing one cannot default to
        # a value that reads as non-destructive.
        if "change" not in entry:
            raise PlanProjectionError("each resource change must carry a change object")
        change = entry["change"]
        if not isinstance(change, Mapping):
            raise PlanProjectionError("resource change `change` must be a mapping")
        actions = change.get("actions")
        if not isinstance(actions, Sequence) or isinstance(actions, str | bytes):
            raise PlanProjectionError("resource change `change.actions` must be a list")
        projected_entry: dict[str, object] = {
            field: entry.get(field) for field in _TOP_LEVEL_FIELDS
        }
        projected_entry["change"] = {field: change.get(field) for field in _CHANGE_FIELDS}
        projected.append(projected_entry)

    projected.sort(key=_address_sort_key)
    return projected


def canonical_plan_bytes(show_json: Mapping[str, object]) -> bytes:
    """Return the canonical UTF-8 bytes hashed into `plan_hash` (ADR-0019 §1).

    Canonical rules: `address`-sorted changes, sorted keys, UTF-8, `(",", ":")`
    separators, non-ASCII escaped, no trailing newline, and NaN/Infinity rejected.
    """
    projected = project_plan_changes(show_json)
    try:
        text = json.dumps(
            projected,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    except ValueError as error:
        raise PlanProjectionError("plan changes must not contain NaN or Infinity") from error
    return text.encode("utf-8")


def compute_plan_hash(show_json: Mapping[str, object]) -> str:
    """Return the SHA-256 hex digest of the canonical plan projection."""
    return hashlib.sha256(canonical_plan_bytes(show_json)).hexdigest()


def has_destructive_changes(show_json: Mapping[str, object]) -> bool:
    """Return whether any projected change deletes or replaces a resource.

    Destructive means `change.actions` contains `delete` or `change.replace_paths`
    is non-empty. This is the sole basis of `PlanReadinessInput.has_destructive_changes`
    and of the `DESTRUCTIVE_CHANGE_REQUIRES_MANUAL_REVIEW` gate (ADR-0019 §1).
    """
    for entry in project_plan_changes(show_json):
        change = entry["change"]
        assert isinstance(change, dict)
        # `project_plan_changes` already guarantees `actions` is a non-string list.
        actions = change["actions"]
        if "delete" in actions:
            return True
        replace_paths = change.get("replace_paths")
        if isinstance(replace_paths, Sequence) and not isinstance(replace_paths, str | bytes):
            if len(replace_paths) > 0:
                return True
    return False


# Terraform resource type → the projected attribute carrying the AWS resource id
# that Findings use (ADR-0019 §1 addendum). An allow-list, for the same reason the
# field projection is one: a provider that adds a new identity-looking attribute must
# never silently change which resource a plan is judged to touch.
#
# The attribute per type is the one whose value equals
# `AwsResourceQuery(resource_type=...).resource_id` for that type, which is what
# `Finding.resource_id` holds:
#
# * S3 — every entry is `bucket`: the bucket resource takes the name as its `bucket`
#   argument and each sub-resource references its parent by the same attribute.
# * EC2 Instance — `id` is the `i-…` instance id. On a create it is computed and
#   therefore skipped, which is correct: a resource that does not exist yet has no
#   Finding about it.
# * RDS DB instance — `identifier` is the customer-declared DB instance identifier and
#   is known in the plan, unlike the computed `arn`.
# * ALB — the load balancer ARN, because a listener can only name its parent by
#   `load_balancer_arn`. Projecting the ARN keeps the load balancer and its listeners in
#   one vocabulary, and it is also what the ELBv2 read adapter queries by.
#
# Deliberately absent: `aws_ebs_volume`, `aws_ebs_snapshot`, and standalone
# `aws_security_group*` resources. Their plan-side identity is a volume/snapshot/security
# group id, but EC2 Findings are raised against the instance (Task 9 scope decision), so
# projecting those ids would answer a different question than the one readiness asks. A
# plan that only changes those resources leaves readiness `BLOCKED` — fail-closed, and a
# documented boundary rather than a silent mismatch.
_RESOURCE_IDENTITY_ATTRIBUTES: dict[str, str] = {
    "aws_s3_bucket": "bucket",
    "aws_s3_bucket_acl": "bucket",
    "aws_s3_bucket_logging": "bucket",
    "aws_s3_bucket_ownership_controls": "bucket",
    "aws_s3_bucket_policy": "bucket",
    "aws_s3_bucket_public_access_block": "bucket",
    "aws_s3_bucket_server_side_encryption_configuration": "bucket",
    "aws_s3_bucket_versioning": "bucket",
    "aws_instance": "id",
    "aws_db_instance": "identifier",
    "aws_lb": "arn",
    # `aws_alb` is the provider's retained alias for the same load balancer resource.
    "aws_alb": "arn",
    "aws_lb_listener": "load_balancer_arn",
    "aws_alb_listener": "load_balancer_arn",
}


def mapped_resource_ids(show_json: Mapping[str, object]) -> tuple[str, ...]:
    """Return the AWS resource ids this plan touches, in the Finding vocabulary.

    `PlanReadinessInput.mapped_resource_ids` answers one question: does this plan
    actually change the resource the Finding is about? Answering it needs the plan's
    Terraform addresses translated into the ids Findings carry (for S3, the bucket
    name), because the two vocabularies do not otherwise meet.

    A resource type outside the allow-list contributes nothing. That is deliberate and
    fail-closed: an unmapped finding resource makes readiness `BLOCKED` rather than
    approving a plan whose relevance we could not establish. Adding a resource type is
    a documented change made alongside the Rule that needs it.

    `after` is read first and `before` second so a destroyed resource still names
    itself. A value that is absent or computed (`after_unknown`) is skipped rather than
    guessed — an unknown id is not evidence that the plan touches the finding.
    """
    identifiers: set[str] = set()
    for entry in project_plan_changes(show_json):
        attribute = _RESOURCE_IDENTITY_ATTRIBUTES.get(_resource_type(entry))
        if attribute is None:
            continue
        change = entry["change"]
        assert isinstance(change, dict)
        for state in ("after", "before"):
            value = change.get(state)
            if not isinstance(value, Mapping):
                continue
            candidate = value.get(attribute)
            if isinstance(candidate, str) and candidate.strip():
                identifiers.add(candidate)
                break
    return tuple(sorted(identifiers))


def _resource_type(entry: Mapping[str, object]) -> str:
    resource_type = entry.get("type")
    if not isinstance(resource_type, str) or not resource_type:
        raise PlanProjectionError("each resource change must carry a non-empty type")
    return resource_type


def _address_sort_key(entry: Mapping[str, object]) -> str:
    address = entry.get("address")
    if not isinstance(address, str) or not address:
        raise PlanProjectionError("each resource change must carry a non-empty address")
    return address
