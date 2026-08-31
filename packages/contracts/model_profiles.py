"""Approved, role-specific Bedrock model profile contract."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string


class ModelProfileRole(StrEnum):
    PARENT = "PARENT"
    ASSESSMENT = "ASSESSMENT"
    REMEDIATION = "REMEDIATION"
    DEPLOYMENT = "DEPLOYMENT"


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelProfile:
    """An approved model and version set that a workflow may invoke."""

    model_profile_id: str
    role: ModelProfileRole
    region: str
    model_id: str
    prompt_version: str
    rubric_version: str
    golden_dataset_version: str

    def __post_init__(self) -> None:
        for field_name in (
            "model_profile_id",
            "region",
            "model_id",
            "prompt_version",
            "rubric_version",
            "golden_dataset_version",
        ):
            require_non_empty_string(getattr(self, field_name), field_name)
        if not isinstance(self.role, ModelProfileRole):
            raise TypeError("role must be a ModelProfileRole")

    def to_dict(self) -> dict[str, str]:
        return {
            "model_profile_id": self.model_profile_id,
            "role": self.role.value,
            "region": self.region,
            "model_id": self.model_id,
            "prompt_version": self.prompt_version,
            "rubric_version": self.rubric_version,
            "golden_dataset_version": self.golden_dataset_version,
        }
