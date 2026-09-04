"""A producer: M4 Golden customer-runtime observation bundle (ADR-0022 §1–§4).

`release_quality.py`(C consumer)는 `m4-golden-observations-v1` bundle을 검증해 sanitized report를 만들지만,
그 bundle을 **만드는** 쪽은 지금까지 코드에 없었다. 이 모듈이 그 producer다.

- Post-Deploy 18 Case(6 Rule × IAC/AWS_ACTUAL/DRIFT)를 `REQUIRED_REPETITIONS`회 반복한다.
- IAC/AWS_ACTUAL은 운영과 같은 `BedrockStructuredEvaluator`로 평가하고, 호출마다 latency와 token
  사용량을 기록한다. DRIFT는 같은 `(Rule, run_number)`의 두 결과에서 `derive_drift_results()`로
  파생한다 — Bedrock을 부르지 않는다(ADR-0022 §2).
- Bundle에는 식별자와 digest만 남는다. resource ID·snapshot 원문·prompt·응답·rationale·provider
  message는 어디에도 쓰지 않는다(§3). provider 실패는 안정된 `error_code`로만 기록한다.
- 실행 진위는 문자열이 증명하지 않는다. `runtime_mode`는 호출자가 선언하며, 보호된 customer
  runtime 밖의 실행은 `DRY_RUN`으로 표시해 C gate가 거부하게 한다.

자격 증명·네트워크는 주입된 client/snapshot reader에만 있다. 이 모듈은 순수 조립 로직이다.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from apps.backend.assessment.bedrock import (
    BedrockConverseClient,
    BedrockEvaluationError,
    BedrockStructuredEvaluator,
)
from apps.backend.assessment.drift import derive_drift_results
from apps.backend.assessment.release_quality import (
    OBSERVATION_SCHEMA_VERSION,
    REQUIRED_REPETITIONS,
    ReleaseGoldenCase,
)
from apps.backend.policy import PolicyContext
from packages.contracts import (
    DecisionSource,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    GoldenDatasetCase,
    ModelProfile,
    PolicyRule,
)

CUSTOMER_SANDBOX_RUNTIME_MODE = "CUSTOMER_SANDBOX"
#: 보호된 customer runtime 밖의 실행. C gate는 이 값을 거부하므로 release evidence로 오인될 수 없다.
DRY_RUN_RUNTIME_MODE = "DRY_RUN"
RUNTIME_MODES = frozenset({CUSTOMER_SANDBOX_RUNTIME_MODE, DRY_RUN_RUNTIME_MODE})

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SNAPSHOT_KEYS = frozenset({"resource_id", "resource_document", "evidence_references"})
_ERROR_CODE_CHARACTERS = re.compile(r"[^A-Z0-9]+")


class GoldenObservationError(ValueError):
    """Raised when the export cannot proceed without producing misleading evidence."""


# --- snapshots -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenSnapshot:
    """One immutable Golden input: the document the evaluator sees plus its content digest."""

    artifact_id: str
    resource_id: str
    resource_document: Mapping[str, object]
    evidence_references: tuple[str, ...]
    content_sha256: str


class GoldenSnapshotReader(Protocol):
    def read(self, artifact_id: str) -> GoldenSnapshot: ...


def parse_golden_snapshot(artifact_id: str, content: bytes) -> GoldenSnapshot:
    """Parse the exact snapshot document shape; anything else is rejected before a model call."""
    if not isinstance(artifact_id, str) or not artifact_id.strip():
        raise GoldenObservationError("artifact_id must be a non-empty string")
    if not isinstance(content, bytes):
        raise GoldenObservationError(f"snapshot {artifact_id} must be bytes")
    try:
        data = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GoldenObservationError(f"snapshot {artifact_id} is not a JSON document") from error
    if not isinstance(data, dict) or set(data) != _SNAPSHOT_KEYS:
        raise GoldenObservationError(f"snapshot {artifact_id} has unexpected fields")
    resource_id = data["resource_id"]
    document = data["resource_document"]
    references = data["evidence_references"]
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise GoldenObservationError(f"snapshot {artifact_id} resource_id is invalid")
    if not isinstance(document, dict):
        raise GoldenObservationError(f"snapshot {artifact_id} resource_document is invalid")
    if (
        not isinstance(references, list)
        or not references
        or any(not isinstance(item, str) or not item.strip() for item in references)
    ):
        raise GoldenObservationError(f"snapshot {artifact_id} evidence_references are invalid")
    return GoldenSnapshot(
        artifact_id=artifact_id,
        resource_id=resource_id,
        resource_document=document,
        evidence_references=tuple(references),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


class DirectoryGoldenSnapshotReader:
    """Read `{artifact_id}.json` files from a local directory (dry-run and local checks)."""

    def __init__(self, directory: Path) -> None:
        if not isinstance(directory, Path):
            raise TypeError("directory must be a Path")
        self._directory = directory

    def read(self, artifact_id: str) -> GoldenSnapshot:
        path = self._directory / f"{artifact_id}.json"
        if not path.is_file():
            raise GoldenObservationError(f"snapshot for {artifact_id} is missing: {path}")
        return parse_golden_snapshot(artifact_id, path.read_bytes())


class ArtifactBytesReader(Protocol):
    """The content-addressed artifact store boundary (`S3ArtifactStore.get`)."""

    def get(self, reference: object) -> bytes: ...


class ArtifactStoreGoldenSnapshotReader:
    """Resolve Golden artifact IDs through a private identifier-only index.

    The index maps `artifact_id` → `sha256:<64 hex>`; the store verifies the bytes against
    that digest, so a tampered or substituted snapshot fails closed instead of being evaluated.
    """

    def __init__(
        self,
        store: ArtifactBytesReader,
        *,
        customer_id: str,
        index: Mapping[str, str],
        reference_factory: object,
    ) -> None:
        if store is None:
            raise TypeError("store is required")
        if not isinstance(customer_id, str) or not customer_id.strip():
            raise ValueError("customer_id must be a non-empty string")
        if not isinstance(index, Mapping) or not index:
            raise GoldenObservationError("snapshot index must be a non-empty mapping")
        for artifact_id, digest in index.items():
            if not isinstance(artifact_id, str) or not artifact_id.strip():
                raise GoldenObservationError("snapshot index keys must be artifact IDs")
            if (
                not isinstance(digest, str)
                or not digest.startswith("sha256:")
                or _SHA256.fullmatch(digest.removeprefix("sha256:")) is None
            ):
                raise GoldenObservationError(
                    f"snapshot index digest for {artifact_id} must be sha256:<64 lowercase hex>"
                )
        self._store = store
        self._customer_id = customer_id
        self._index = dict(index)
        self._reference_factory = reference_factory

    def read(self, artifact_id: str) -> GoldenSnapshot:
        digest = self._index.get(artifact_id)
        if digest is None:
            raise GoldenObservationError(f"snapshot for {artifact_id} is not in the index")
        reference = self._reference_factory(customer_id=self._customer_id, content_digest=digest)
        try:
            content = self._store.get(reference)
        except Exception as error:  # store errors carry no customer content
            raise GoldenObservationError(
                f"snapshot for {artifact_id} could not be read: {type(error).__name__}"
            ) from None
        snapshot = parse_golden_snapshot(artifact_id, content)
        if snapshot.content_sha256 != digest.removeprefix("sha256:"):
            raise GoldenObservationError(f"snapshot for {artifact_id} does not match its digest")
        return snapshot


# --- Bedrock usage ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class BedrockCallUsage:
    latency_ms: int
    input_tokens: int
    output_tokens: int


class UsageRecordingConverseClient:
    """Wrap the regional client so each Converse call leaves its usage behind.

    The evaluator's contract stays untouched: it still receives the provider response. The
    bundle needs latency and token counts per observation, and Converse reports both
    (`metrics.latencyMs`, `usage.inputTokens/outputTokens`). A response without usage is not a
    provider response, so it fails closed rather than being recorded as a zero-cost call.
    """

    def __init__(self, inner: BedrockConverseClient) -> None:
        if inner is None:
            raise TypeError("inner client is required")
        self._inner = inner
        self.last_usage: BedrockCallUsage | None = None
        self.call_count = 0

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.last_usage = None
        self.call_count += 1
        started = time.monotonic()
        response = self._inner.converse(**kwargs)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if not isinstance(response, Mapping):
            raise GoldenObservationError("provider response is not a mapping")
        usage = response.get("usage")
        if not isinstance(usage, Mapping):
            raise GoldenObservationError("provider response lacks usage")
        input_tokens = _non_negative_int(usage.get("inputTokens"), "usage.inputTokens")
        output_tokens = _non_negative_int(usage.get("outputTokens"), "usage.outputTokens")
        metrics = response.get("metrics")
        latency = metrics.get("latencyMs") if isinstance(metrics, Mapping) else None
        latency_ms = (
            latency if isinstance(latency, int) and not isinstance(latency, bool) else elapsed_ms
        )
        self.last_usage = BedrockCallUsage(
            latency_ms=max(latency_ms, 0),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        return response


def stable_error_code(error: BaseException) -> str:
    """Map a provider/model failure to a stable code without leaking its message (§3)."""
    if isinstance(error, GoldenObservationError):
        return "PRODUCER_ERROR"
    if isinstance(error, BedrockEvaluationError):
        return "MODEL_OUTPUT_REJECTED"
    response = getattr(error, "response", None)
    code = None
    if isinstance(response, Mapping):
        details = response.get("Error")
        if isinstance(details, Mapping):
            code = details.get("Code")
    if isinstance(code, str) and code.strip():
        if code == "ThrottlingException":
            return "PROVIDER_THROTTLED"
        return "PROVIDER_" + _ERROR_CODE_CHARACTERS.sub("_", code.upper()).strip("_")
    return "PROVIDER_ERROR"


# --- execution identity --------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class GoldenExecutionIdentity:
    """Top-level bundle identity. Digest inputs come from the D producer (`release_binding`)."""

    scenario_id: str
    runtime_mode: str
    platform_commit_sha: str
    repository_commit_sha256: str
    deployment_id_sha256: str
    artifact_set_sha256: str
    execution_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id.strip():
            raise GoldenObservationError("scenario_id must be a non-empty string")
        if self.runtime_mode not in RUNTIME_MODES:
            raise GoldenObservationError(f"runtime_mode must be one of {sorted(RUNTIME_MODES)}")
        if (
            not isinstance(self.platform_commit_sha, str)
            or _COMMIT_SHA.fullmatch(self.platform_commit_sha) is None
        ):
            raise GoldenObservationError("platform_commit_sha must be a lowercase 40-hex SHA")
        for name in ("repository_commit_sha256", "deployment_id_sha256", "artifact_set_sha256"):
            value = getattr(self, name)
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise GoldenObservationError(f"{name} must be a lowercase SHA-256")
        if not isinstance(self.execution_id, str) or not self.execution_id.strip():
            raise GoldenObservationError("execution_id must be a non-empty string")


def new_execution_id() -> str:
    """Opaque execution ID; carries no customer, account, or run information."""
    return uuid.uuid4().hex


# --- exporter --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class _RuleCoordinate:
    rule: PolicyRule
    rule_version: str
    iac: GoldenDatasetCase
    actual: GoldenDatasetCase
    drift: GoldenDatasetCase


class GoldenObservationExporter:
    """Run the release Golden cases through the production evaluator and emit the bundle."""

    def __init__(
        self,
        *,
        client: BedrockConverseClient,
        profile: ModelProfile,
        cases: Sequence[ReleaseGoldenCase],
        rules: Sequence[PolicyRule],
        policy_profile: tuple[str, str],
        snapshots: GoldenSnapshotReader,
        repetitions: int = REQUIRED_REPETITIONS,
    ) -> None:
        if not isinstance(profile, ModelProfile):
            raise TypeError("profile must be a ModelProfile")
        if snapshots is None:
            raise TypeError("snapshots reader is required")
        if isinstance(repetitions, bool) or not isinstance(repetitions, int) or repetitions < 1:
            raise ValueError("repetitions must be a positive integer")
        self._client = UsageRecordingConverseClient(client)
        self._profile = profile
        self._policy_profile = policy_profile
        self._snapshots = snapshots
        self._repetitions = repetitions
        self._coordinates = _bind_coordinates(tuple(cases), tuple(rules))

    @property
    def bedrock_call_count(self) -> int:
        return self._client.call_count

    def export(
        self,
        identity: GoldenExecutionIdentity,
        *,
        generated_at: datetime | None = None,
    ) -> dict[str, object]:
        """Evaluate every coordinate `repetitions` times and return the bundle as plain JSON data."""
        if not isinstance(identity, GoldenExecutionIdentity):
            raise TypeError("identity must be a GoldenExecutionIdentity")
        inputs = self._preflight_snapshots()
        observations: list[dict[str, object]] = []
        for run_number in range(1, self._repetitions + 1):
            for coordinate in self._coordinates:
                iac_snapshot, actual_snapshot = inputs[coordinate.rule.rule_id]
                iac_result, iac_observation = self._bedrock(
                    coordinate, coordinate.iac, iac_snapshot, run_number
                )
                actual_result, actual_observation = self._bedrock(
                    coordinate, coordinate.actual, actual_snapshot, run_number
                )
                drift_result = derive_drift_results(
                    iac_results=(iac_result,), actual_results=(actual_result,)
                )[0]
                observations.append(iac_observation)
                observations.append(actual_observation)
                observations.append(
                    _observation(
                        case=coordinate.drift,
                        rule_version=coordinate.rule_version,
                        result=drift_result,
                        profile=self._profile,
                        input_artifact_sha256=_drift_input_digest(iac_snapshot, actual_snapshot),
                        execution_kind="CODE_DERIVED",
                        usage=None,
                        error_code=None,
                        run_number=run_number,
                    )
                )
        moment = generated_at if generated_at is not None else datetime.now(UTC)
        if moment.tzinfo is None:
            raise GoldenObservationError("generated_at must be offset-aware")
        return {
            "schema_version": OBSERVATION_SCHEMA_VERSION,
            "execution_id": identity.execution_id,
            "generated_at": moment.isoformat(),
            "scenario_id": identity.scenario_id,
            "runtime_mode": identity.runtime_mode,
            "platform_commit_sha": identity.platform_commit_sha,
            "repository_commit_sha256": identity.repository_commit_sha256,
            "deployment_id_sha256": identity.deployment_id_sha256,
            "artifact_set_sha256": identity.artifact_set_sha256,
            "model_profile": self._profile.to_dict(),
            "observations": observations,
        }

    def _preflight_snapshots(self) -> dict[str, tuple[GoldenSnapshot, GoldenSnapshot]]:
        """Read every input before the first model call so a bad input costs no Bedrock calls."""
        inputs: dict[str, tuple[GoldenSnapshot, GoldenSnapshot]] = {}
        for coordinate in self._coordinates:
            iac = self._snapshots.read(coordinate.iac.resource_snapshot_artifact_id)
            actual = self._snapshots.read(coordinate.actual.resource_snapshot_artifact_id)
            if iac.resource_id != actual.resource_id:
                # DRIFT compares the same resource from two sides; the gate enforces the same
                # resource hash across the triple, so a mismatch can never produce evidence.
                raise GoldenObservationError(
                    f"IAC and AWS_ACTUAL snapshots for {coordinate.rule.rule_id} "
                    "describe different resources"
                )
            inputs[coordinate.rule.rule_id] = (iac, actual)
        return inputs

    def _bedrock(
        self,
        coordinate: _RuleCoordinate,
        case: GoldenDatasetCase,
        snapshot: GoldenSnapshot,
        run_number: int,
    ) -> tuple[EvaluationResult, dict[str, object]]:
        rule = coordinate.rule
        context = PolicyContext(
            policy_profile_id=self._policy_profile[0],
            policy_profile_version=self._policy_profile[1],
            phase=case.phase,
            resource_type=rule.resource_types[0],
            rules=(rule,),
        )
        evaluator = BedrockStructuredEvaluator(
            client=self._client,
            perspective=case.perspective,
            resource_document=snapshot.resource_document,
            evidence_references=snapshot.evidence_references,
        )
        try:
            result = evaluator.evaluate(
                resource_id=snapshot.resource_id,
                rule=rule,
                context=context,
                model_profile=self._profile,
            )
        except GoldenObservationError:
            raise
        except Exception as error:  # provider/model failure → stable code, no message
            result = _execution_error_result(snapshot, rule, case, self._profile)
            usage: BedrockCallUsage | None = None
            error_code: str | None = stable_error_code(error)
        else:
            usage = self._client.last_usage
            error_code = None
            if usage is None:
                raise GoldenObservationError("usage was not recorded for a successful call")
        return result, _observation(
            case=case,
            rule_version=coordinate.rule_version,
            result=result,
            profile=self._profile,
            input_artifact_sha256=snapshot.content_sha256,
            execution_kind="BEDROCK",
            usage=usage,
            error_code=error_code,
            run_number=run_number,
        )


def _bind_coordinates(
    cases: tuple[ReleaseGoldenCase, ...], rules: tuple[PolicyRule, ...]
) -> tuple[_RuleCoordinate, ...]:
    if not cases:
        raise GoldenObservationError("release Golden cases must not be empty")
    by_version = {(rule.rule_id, rule.version): rule for rule in rules}
    grouped: dict[str, dict[EvaluationPerspective, ReleaseGoldenCase]] = {}
    versions: dict[str, str] = {}
    for entry in cases:
        if not isinstance(entry, ReleaseGoldenCase):
            raise TypeError("cases must contain ReleaseGoldenCase values")
        perspectives = grouped.setdefault(entry.rule_id, {})
        if entry.case.perspective in perspectives:
            raise GoldenObservationError(
                f"duplicate {entry.case.perspective.value} case for {entry.rule_id}"
            )
        perspectives[entry.case.perspective] = entry
        if versions.setdefault(entry.rule_id, entry.rule_version) != entry.rule_version:
            raise GoldenObservationError(f"cases for {entry.rule_id} disagree on rule version")
    coordinates: list[_RuleCoordinate] = []
    for rule_id in sorted(grouped):
        perspectives = grouped[rule_id]
        expected = {
            EvaluationPerspective.IAC,
            EvaluationPerspective.AWS_ACTUAL,
            EvaluationPerspective.DRIFT,
        }
        if set(perspectives) != expected:
            raise GoldenObservationError(
                f"{rule_id} needs exactly IAC, AWS_ACTUAL, and DRIFT release cases"
            )
        rule = by_version.get((rule_id, versions[rule_id]))
        if rule is None:
            raise GoldenObservationError(
                f"rule {rule_id} version {versions[rule_id]} is not in the registry"
            )
        if not rule.resource_types:
            raise GoldenObservationError(f"rule {rule_id} declares no resource types")
        coordinates.append(
            _RuleCoordinate(
                rule=rule,
                rule_version=versions[rule_id],
                iac=perspectives[EvaluationPerspective.IAC].case,
                actual=perspectives[EvaluationPerspective.AWS_ACTUAL].case,
                drift=perspectives[EvaluationPerspective.DRIFT].case,
            )
        )
    return tuple(coordinates)


def _execution_error_result(
    snapshot: GoldenSnapshot, rule: PolicyRule, case: GoldenDatasetCase, profile: ModelProfile
) -> EvaluationResult:
    """The failed side of a DRIFT pair; DRIFT derivation propagates EXECUTION_ERROR from it."""
    return EvaluationResult(
        resource_id=snapshot.resource_id,
        rule_id=rule.rule_id,
        perspective=case.perspective,
        status=EvaluationStatus.EXECUTION_ERROR,
        severity=rule.severity.value,
        score=0.0,
        rationale="Provider call failed; recorded by stable error code only.",
        evidence_references=(),
        rule_version=rule.version,
        rubric_version=profile.rubric_version,
        model_profile_id=profile.model_profile_id,
        scoring_mode=case.scoring_mode,
        decided_by=DecisionSource.CODE,
    )


def _observation(
    *,
    case: GoldenDatasetCase,
    rule_version: str,
    result: EvaluationResult,
    profile: ModelProfile,
    input_artifact_sha256: str,
    execution_kind: str,
    usage: BedrockCallUsage | None,
    error_code: str | None,
    run_number: int,
) -> dict[str, object]:
    successful = error_code is None
    return {
        "case_id": case.case_id,
        "run_number": run_number,
        "rule_id": result.rule_id,
        "rule_version": rule_version,
        "phase": case.phase.value,
        "perspective": case.perspective.value,
        "status": result.status.value,
        "severity": result.severity,
        "score": float(result.score),
        "evidence_references": list(result.evidence_references),
        "model_profile_id": profile.model_profile_id,
        "rubric_version": profile.rubric_version,
        "scoring_mode": case.scoring_mode.value,
        "resource_id_sha256": _sha256_text(result.resource_id),
        "input_artifact_sha256": input_artifact_sha256,
        "evaluation_output_sha256": _output_digest(result) if successful else None,
        "execution_kind": execution_kind,
        "latency_ms": usage.latency_ms if usage is not None else None,
        "input_tokens": usage.input_tokens if usage is not None else None,
        "output_tokens": usage.output_tokens if usage is not None else None,
        "error_code": error_code,
    }


def _output_digest(result: EvaluationResult) -> str:
    """Digest of the validated model output so a reviewer can match the private raw record."""
    canonical = json.dumps(
        {
            "status": result.status.value,
            "score": float(result.score),
            "rationale": result.rationale,
            "evidence_references": sorted(result.evidence_references),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _drift_input_digest(iac: GoldenSnapshot, actual: GoldenSnapshot) -> str:
    """DRIFT consumes both snapshots; bind it to the ordered pair of their content digests."""
    return _sha256_text(f"{iac.content_sha256}:{actual.content_sha256}")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _non_negative_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GoldenObservationError(f"provider response {name} must be a non-negative integer")
    return value
