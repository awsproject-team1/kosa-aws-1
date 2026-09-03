"""The extraction boundary: what an extractor is, and a fake that decides nothing.

Extractor는 정규화된 unit과 Catalog를 받아 Requirement 후보를 돌려주는 것 **뿐**이다. 무엇이
승인 가능한지, 어떤 severity를 갖는지, 어떤 Rule이 되는지는 뒤따르는 검증과 Rule Builder가
정한다. 그 경계를 지키기 위해 반환 타입은 `ExtractedRequirement`이고, 그 타입에는 평가 결과
필드가 없다.

`FakePolicyCandidateExtractor`는 **주입된 결과만 돌려준다.** 정책 text 문자열을 검사해 분기하면
테스트가 통과하는 이유가 "파이프라인이 옳다"가 아니라 "가짜가 그 문장을 알아봤다"가 된다. 그런
가짜는 실제 모델로 바꿨을 때 아무것도 보장하지 못한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from apps.backend.policy.authoring.artifact_reader import ExtractionUnit
from packages.contracts import (
    CANDIDATE_SCHEMA_VERSION,
    AuthoringProvenance,
    ExtractedRequirement,
    GovernanceControlCatalog,
    NormalizedPolicyDocument,
)
from packages.contracts._validation import require_non_empty_string


@dataclass(frozen=True, slots=True, kw_only=True)
class ExtractorIdentity:
    """Who produced an extraction, and with which prompt and model.

    `candidate_schema_version`과 `control_catalog_version`은 여기 없다 — 전자는 Contract가,
    후자는 넘어온 Catalog가 정한다. Extractor가 그 둘을 스스로 주장하게 두면, 실제로 사용한
    schema/Catalog와 다른 값을 기록할 수 있다.
    """

    extractor_id: str
    extractor_version: str
    model_id: str
    model_version: str
    prompt_version: str

    def __post_init__(self) -> None:
        for name in (
            "extractor_id",
            "extractor_version",
            "model_id",
            "model_version",
            "prompt_version",
        ):
            require_non_empty_string(getattr(self, name), name)

    def provenance(
        self,
        *,
        catalog: GovernanceControlCatalog,
        authoring_run_id: str,
        requested_at: str,
    ) -> AuthoringProvenance:
        """Compose the full provenance from the identity plus this run's facts."""
        return AuthoringProvenance(
            extractor_id=self.extractor_id,
            extractor_version=self.extractor_version,
            model_id=self.model_id,
            model_version=self.model_version,
            prompt_version=self.prompt_version,
            candidate_schema_version=CANDIDATE_SCHEMA_VERSION,
            control_catalog_version=catalog.version,
            authoring_run_id=authoring_run_id,
            requested_at=requested_at,
        )


class PolicyCandidateExtractor(Protocol):
    """Produce Requirement candidates from verified policy text within a Catalog boundary."""

    @property
    def identity(self) -> ExtractorIdentity: ...

    def extract(
        self,
        *,
        document: NormalizedPolicyDocument,
        units: tuple[ExtractionUnit, ...],
        catalog: GovernanceControlCatalog,
    ) -> tuple[ExtractedRequirement, ...]: ...


class FakePolicyCandidateExtractor:
    """Return exactly the requirements it was given — never anything derived from the text.

    파이프라인 테스트가 검사해야 하는 것은 "추출 결과가 어떻게 검증되고 Rule이 되는가"이지
    "모델이 문장을 알아보는가"가 아니다. 그래서 이 가짜는 unit 텍스트를 읽지 않는다.
    """

    def __init__(
        self,
        results: Sequence[ExtractedRequirement] = (),
        *,
        identity: ExtractorIdentity | None = None,
    ) -> None:
        for entry in results:
            if not isinstance(entry, ExtractedRequirement):
                raise TypeError("results must contain ExtractedRequirement values")
        self._results = tuple(results)
        self._identity = identity or ExtractorIdentity(
            extractor_id="fake-policy-candidate-extractor",
            extractor_version="1.0.0",
            model_id="fake",
            model_version="1",
            prompt_version="policy-authoring/fake",
        )
        #: 호출 인자를 기록해 배선을 검사할 수 있게 한다. 텍스트는 기록하지 않는다.
        self.calls: list[tuple[str, str, int, str]] = []

    @property
    def identity(self) -> ExtractorIdentity:
        return self._identity

    def extract(
        self,
        *,
        document: NormalizedPolicyDocument,
        units: tuple[ExtractionUnit, ...],
        catalog: GovernanceControlCatalog,
    ) -> tuple[ExtractedRequirement, ...]:
        if not isinstance(document, NormalizedPolicyDocument):
            raise TypeError("document must be a NormalizedPolicyDocument")
        if not isinstance(catalog, GovernanceControlCatalog):
            raise TypeError("catalog must be a GovernanceControlCatalog")
        if not units or not all(isinstance(unit, ExtractionUnit) for unit in units):
            raise ValueError("units must contain at least one ExtractionUnit")
        self.calls.append(
            (document.source_id, document.source_version, len(units), catalog.version)
        )
        return self._results
