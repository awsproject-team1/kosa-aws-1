"""Run the Assessment Golden Dataset through the production evaluator, repeatedly.

`bench/`는 모델 선정용이며 자체 prompt로 Bedrock을 직접 부른다 — Runtime 평가기와 다른 계약이다.
이 스크립트는 **Runtime이 쓰는 `BedrockStructuredEvaluator`와 승인 Model Profile을 그대로** 써서
Golden case를 반복 실행하고, `GoldenDatasetRunner`의 품질 지표(status/score/evidence 정확도, 동일
Case 일치율, score 편차)를 낸다. ADR-0021 gate의 "prompt·Profile이 바뀌면 재실행" 근거는 이것으로
만든다.

입력
- `--cases`      Golden case JSON (`fixtures/m1/golden_dataset_cases.json` 형식)
- `--snapshots`  case의 `resource_snapshot_artifact_id`마다 `{artifact_id}.json`이 있는 디렉터리.
                 각 파일은 평가기에 넘길 근거 문서다:
                 {"resource_id": "...", "resource_document": {...},
                  "evidence_references": ["terraform:...", "aws:..."]}
                 이 파일은 고객 데이터를 담을 수 있으므로 저장소에 커밋하지 않는다.
- `--profile`    승인 Assessment Model Profile JSON (`fixtures/m1/assessment_model_profile.json`)
- `--rules`      Rule Registry 디렉터리 (`fixtures/rules`) — case의 `rule_id`로 Rule을 고른다
- `--repetitions` 반복 횟수(기본 5)
- `--dry-run`    Bedrock 대신 case의 기대값을 돌려주는 fake client로 배관만 검증한다

출력은 case별 지표와 전체 통과 여부이며, 응답 원문은 남기지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.backend.assessment.bedrock import BedrockStructuredEvaluator  # noqa: E402
from apps.backend.assessment.quality import GoldenDatasetRunner  # noqa: E402
from apps.backend.policy import PolicyContext  # noqa: E402
from apps.backend.policy.registry import load_rule_registry  # noqa: E402
from packages.contracts import (  # noqa: E402
    AssessmentPhase,
    EvaluationPerspective,
    EvaluationResult,
    EvaluationStatus,
    GoldenDatasetCase,
    ModelProfile,
    ModelProfileRole,
    PolicyRule,
    ScoringMode,
)


class GoldenRunError(RuntimeError):
    """The Golden run could not be assembled from the supplied inputs."""


class ProductionGoldenEvaluator:
    """`GoldenCaseEvaluator` over the production Bedrock adapter and one approved Profile."""

    def __init__(
        self,
        *,
        client: object,
        profile: ModelProfile,
        rules_by_case: Mapping[str, PolicyRule],
        snapshots: Path,
        policy_profile: tuple[str, str],
    ) -> None:
        self._client = client
        self._profile = profile
        self._rules_by_case = rules_by_case
        self._snapshots = snapshots
        self._policy_profile = policy_profile

    def evaluate_case(self, case: GoldenDatasetCase) -> EvaluationResult:
        if case.perspective is EvaluationPerspective.DRIFT:
            raise GoldenRunError("DRIFT is derived in code, not evaluated by the model")
        rule = self._rules_by_case[case.case_id]
        snapshot = _load_snapshot(self._snapshots, case.resource_snapshot_artifact_id)
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
            resource_document=snapshot["resource_document"],
            evidence_references=tuple(snapshot["evidence_references"]),
        )
        return evaluator.evaluate(
            resource_id=snapshot["resource_id"],
            rule=rule,
            context=context,
            model_profile=self._profile,
        )


class DryRunClient:
    """Answers with each case's expected outcome so the plumbing can be checked offline."""

    def __init__(self, cases: Mapping[str, GoldenDatasetCase]) -> None:
        self._cases = cases

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        rule_id = body["rule"]["rule_id"]
        perspective = body["perspective"]
        case = next(
            case
            for case in self._cases.values()
            if case.perspective.value == perspective
            and case.case_id.startswith("golden-")
            and _rule_of(case) == rule_id
        )
        return {
            "output": {
                "message": {
                    "content": [
                        {
                            "text": json.dumps(
                                {
                                    "status": case.expected_status.value,
                                    "score": case.expected_score_min,
                                    "rationale": "dry run",
                                    "evidence_references": list(case.expected_evidence_references),
                                }
                            )
                        }
                    ]
                }
            }
        }


_CASE_RULES: dict[str, str] = {}


def _rule_of(case: GoldenDatasetCase) -> str:
    return _CASE_RULES[case.case_id]


def load_cases(path: Path) -> dict[str, GoldenDatasetCase]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise GoldenRunError("cases file must be a list")
    cases: dict[str, GoldenDatasetCase] = {}
    for entry in raw:
        case = GoldenDatasetCase(
            case_id=entry["case_id"],
            phase=AssessmentPhase(entry["phase"]),
            perspective=EvaluationPerspective(entry["perspective"]),
            rubric_version=entry["rubric_version"],
            scoring_mode=ScoringMode(entry["scoring_mode"]),
            resource_snapshot_artifact_id=entry["resource_snapshot_artifact_id"],
            expected_status=EvaluationStatus(entry["expected_status"]),
            expected_score_min=entry["expected_score_min"],
            expected_score_max=entry["expected_score_max"],
            expected_evidence_references=tuple(entry["expected_evidence_references"]),
        )
        cases[case.case_id] = case
        _CASE_RULES[case.case_id] = entry["rule_id"]
    return cases


def load_profile(path: Path) -> ModelProfile:
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = ModelProfile(
        model_profile_id=data["model_profile_id"],
        role=ModelProfileRole(data["role"]),
        region=data["region"],
        model_id=data["model_id"],
        prompt_version=data["prompt_version"],
        rubric_version=data["rubric_version"],
        golden_dataset_version=data["golden_dataset_version"],
    )
    if profile.role is not ModelProfileRole.ASSESSMENT:
        raise GoldenRunError("profile is not an ASSESSMENT profile")
    return profile


def _load_snapshot(directory: Path, artifact_id: str) -> Mapping[str, object]:
    path = directory / f"{artifact_id}.json"
    if not path.is_file():
        raise GoldenRunError(f"snapshot for {artifact_id} is missing: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping) or set(data) != {
        "resource_id",
        "resource_document",
        "evidence_references",
    }:
        raise GoldenRunError(f"snapshot {artifact_id} has unexpected fields")
    return data


def build_evaluator(
    *,
    cases: Mapping[str, GoldenDatasetCase],
    profile: ModelProfile,
    rules_path: Path,
    snapshots: Path,
    client: object,
) -> ProductionGoldenEvaluator:
    registry = load_rule_registry(rules_path)
    rules_by_case: dict[str, PolicyRule] = {}
    for case_id, case in cases.items():
        rule_id = _CASE_RULES[case_id]
        matches = [rule for rule in registry.rules if rule.rule_id == rule_id]
        if not matches:
            raise GoldenRunError(f"rule {rule_id} for case {case_id} is not in the registry")
        rules_by_case[case_id] = sorted(matches, key=lambda rule: rule.version)[-1]
        if case.rubric_version != profile.rubric_version:
            raise GoldenRunError(
                f"case {case_id} rubric {case.rubric_version} differs from the profile rubric"
            )
    profiles = registry.profiles
    if not profiles:
        raise GoldenRunError("registry has no policy profile")
    policy_profile = (profiles[0].policy_profile_id, profiles[0].version)
    return ProductionGoldenEvaluator(
        client=client,
        profile=profile,
        rules_by_case=rules_by_case,
        snapshots=snapshots,
        policy_profile=policy_profile,
    )


def run(
    *,
    cases: Mapping[str, GoldenDatasetCase],
    evaluator: ProductionGoldenEvaluator,
    repetitions: int,
) -> list[dict[str, object]]:
    runner = GoldenDatasetRunner(evaluator)
    reports = []
    for case in cases.values():
        if case.perspective is EvaluationPerspective.DRIFT:
            continue  # DRIFT는 두 결과에서 코드가 파생한다; 모델 반복 평가 대상이 아니다.
        report = runner.evaluate(case, repetitions=repetitions)
        reports.append(
            {
                "case_id": report.case_id,
                "runs": report.runs,
                "status_accuracy": report.status_accuracy,
                "score_accuracy": report.score_accuracy,
                "evidence_accuracy": report.evidence_accuracy,
                "same_case_agreement": report.same_case_agreement,
                "score_spread": report.score_spread,
                "passes": report.passes_m0_gate,
            }
        )
    return reports


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--cases", type=Path, default=ROOT / "fixtures/m1/golden_dataset_cases.json"
    )
    parser.add_argument("--snapshots", type=Path, required=True)
    parser.add_argument(
        "--profile", type=Path, default=ROOT / "fixtures/m1/assessment_model_profile.json"
    )
    parser.add_argument("--rules", type=Path, default=ROOT / "fixtures/rules")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    profile = load_profile(args.profile)
    if args.dry_run:
        client: object = DryRunClient(cases)
    else:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=profile.region)
    evaluator = build_evaluator(
        cases=cases,
        profile=profile,
        rules_path=args.rules,
        snapshots=args.snapshots,
        client=client,
    )
    reports = run(cases=cases, evaluator=evaluator, repetitions=args.repetitions)
    summary = {
        "model_profile_id": profile.model_profile_id,
        "prompt_version": profile.prompt_version,
        "rubric_version": profile.rubric_version,
        "repetitions": args.repetitions,
        "dry_run": args.dry_run,
        "cases": reports,
        "passes": all(report["passes"] for report in reports) and bool(reports),
    }
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if summary["passes"] else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
