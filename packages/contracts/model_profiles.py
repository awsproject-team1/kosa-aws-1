"""Approved, role-specific Bedrock model profile contract."""

from dataclasses import dataclass
from enum import StrEnum

from packages.contracts._validation import require_non_empty_string


class ModelProfileRole(StrEnum):
    """Which workflow a model profile is approved for.

    역할을 나누는 이유는 prompt와 rubric이 역할마다 다르기 때문만이 아니다. 한 profile이 모든
    역할에 쓰이면, Assessment용으로 승인된 모델이 정책 추출에도 쓰이고 그 반대도 가능해진다 —
    승인 경계가 역할별로 존재하지 않게 된다.
    """

    PARENT = "PARENT"
    ASSESSMENT = "ASSESSMENT"
    REMEDIATION = "REMEDIATION"
    DEPLOYMENT = "DEPLOYMENT"
    POLICY_AUTHORING = "POLICY_AUTHORING"


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
