"""Durable selector state for an Assessment workflow."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class Assessment:
    """Assessment selectors persisted before its workflow task is dispatched."""

    assessment_id: str
    customer_id: str
    job_id: str
    repository_id: str
    policy_profile_id: str

    def __post_init__(self) -> None:
        for name in (
            "assessment_id",
            "customer_id",
            "job_id",
            "repository_id",
            "policy_profile_id",
        ):
            _require_non_empty_string(getattr(self, name), name)


def _require_non_empty_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
