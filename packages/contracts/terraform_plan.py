"""Deterministic `terraform show -json` projection shared by A, C, and D.

`plan_hash` is the SHA-256 of an allow-list projection of the plan's
`resource_changes[]`, not of the raw `show -json` output and not of the saved
binary plan (ADR-0019 §1). D produces the value, A re-checks it at approval, C
binds it to readiness, and D re-verifies it immediately before apply. Because
all four call the *same* projection here, the value cannot drift between roles.

The projection is defined as an allow-list, not an exclude-list: a new Terraform
or provider output field must never silently enter the hash and break
reproducibility. `has_destructive_changes` is derived from the same projected
changes so the destructive-change gate reads exactly what was hashed.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence

# The eleven fields kept from each `resource_changes[]` entry (ADR-0019 §1). Any
# other field (for example `timestamp`, `format_version`, `terraform_version`,
# `prior_state`) is dropped by omission, not by an exclude rule.
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
    resource_changes = show_json.get("resource_changes", [])
    if not isinstance(resource_changes, Sequence) or isinstance(resource_changes, str | bytes):
        raise PlanProjectionError("resource_changes must be a list")

    projected: list[dict[str, object]] = []
    for entry in resource_changes:
        if not isinstance(entry, Mapping):
            raise PlanProjectionError("each resource change must be a mapping")
        change = entry.get("change", {})
        if not isinstance(change, Mapping):
            raise PlanProjectionError("resource change `change` must be a mapping")
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
        actions = change.get("actions")
        if isinstance(actions, Sequence) and not isinstance(actions, str | bytes):
            if "delete" in actions:
                return True
        replace_paths = change.get("replace_paths")
        if isinstance(replace_paths, Sequence) and not isinstance(replace_paths, str | bytes):
            if len(replace_paths) > 0:
                return True
    return False


def _address_sort_key(entry: Mapping[str, object]) -> str:
    address = entry.get("address")
    if not isinstance(address, str) or not address:
        raise PlanProjectionError("each resource change must carry a non-empty address")
    return address
