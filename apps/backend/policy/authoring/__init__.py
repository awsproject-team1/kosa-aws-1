"""Policy authoring: turn an approved normalized document into reviewable Rule candidates.

이 패키지는 사람이 승인할 **후보**만 만든다. 승인·게시·평가는 기존 경로가 그대로 담당한다
(`approve_source`, `publish_profile`, `AssessmentRunner`). 두 번째 Assessment Engine을 만들지 않는다.
"""

from apps.backend.policy.authoring.artifact_reader import (
    MAX_NORMALIZED_ARTIFACT_BYTES,
    ArtifactReadError,
    ExtractionUnit,
    NormalizedArtifactReader,
)
from apps.backend.policy.authoring.bedrock_extractor import (
    PROMPT_VERSION,
    BedrockExtractionError,
    BedrockPolicyCandidateExtractor,
)
from apps.backend.policy.authoring.extractor import (
    ExtractorIdentity,
    FakePolicyCandidateExtractor,
    PolicyCandidateExtractor,
)
from apps.backend.policy.authoring.pipeline import (
    DuplicateRequirementError,
    extract_policy_candidates,
)
from apps.backend.policy.authoring.rule_builder import APPLICABLE_PHASES, build_candidate

__all__ = [
    "APPLICABLE_PHASES",
    "MAX_NORMALIZED_ARTIFACT_BYTES",
    "PROMPT_VERSION",
    "ArtifactReadError",
    "BedrockExtractionError",
    "BedrockPolicyCandidateExtractor",
    "DuplicateRequirementError",
    "ExtractionUnit",
    "ExtractorIdentity",
    "FakePolicyCandidateExtractor",
    "NormalizedArtifactReader",
    "PolicyCandidateExtractor",
    "build_candidate",
    "extract_policy_candidates",
]
