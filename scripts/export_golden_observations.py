"""Export the M4 Golden observation bundle (ADR-0022 A producer).

이 CLI는 Post-Deploy 18 Case를 운영 평가기로 5회 반복 실행해 `m4-golden-observations-v1` bundle을
쓴다. 그 bundle을 `scripts/evaluate_m4_golden_release_gate.py --observations`에 넘기면 C gate가
sanitized report를 만든다.

두 실행 방식이 있다.

- 기본(dry run): AWS 없이 기대값을 되돌려 주는 client로 배관만 검사한다. bundle의 `runtime_mode`는
  `DRY_RUN`이며 C gate는 이를 거부한다 — 로컬 실행이 release evidence로 오인될 수 없다.
- `--customer-sandbox`: 보호된 customer runtime에서 실제 Bedrock과 S3 artifact store를 쓴다.
  snapshot은 private identifier-only index(`artifact_id` → `sha256:<hex>`)로 해석하고, D producer의
  세 결합 digest는 `--demo-commit-sha`/`--deployment-id`/`--artifact-sha256`에서 계산한다.

출력 bundle은 private input이다. Git에 커밋하지 않고 고객 승인 저장소에 보관한다(ADR-0022 §3).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.backend.assessment.golden_observations import (  # noqa: E402
    CUSTOMER_SANDBOX_RUNTIME_MODE,
    DRY_RUN_RUNTIME_MODE,
    ArtifactStoreGoldenSnapshotReader,
    DirectoryGoldenSnapshotReader,
    GoldenExecutionIdentity,
    GoldenObservationError,
    GoldenObservationExporter,
    GoldenSnapshotReader,
    new_execution_id,
)
from apps.backend.assessment.release_quality import (  # noqa: E402
    REQUIRED_REPETITIONS,
    GoldenReleaseQualityError,
    ReleaseGoldenCase,
    load_approved_model_profile,
    load_golden_observation_bundle,
    load_release_golden_cases,
)
from apps.backend.deployment.release_binding import (  # noqa: E402
    ReleaseBindingError,
    derive_release_binding,
)
from apps.backend.policy import load_rule_registry  # noqa: E402
from apps.backend.policy.demo import (  # noqa: E402
    load_demo_policy_coverage,
    validate_demo_policy_coverage,
)

MANIFEST = ROOT / "fixtures" / "m4" / "demo_policy_coverage.json"
CASES = ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json"
INITIAL_CASES = ROOT / "fixtures" / "m1" / "golden_dataset_cases.json"
PROFILE = ROOT / "fixtures" / "m1" / "assessment_model_profile.json"
RULES = ROOT / "fixtures" / "rules"

_PLACEHOLDER_COMMIT = "0" * 40
_PLACEHOLDER_ARTIFACT = "0" * 64


class ExpectedOutcomeClient:
    """Dry-run provider: answers each case with its expected outcome plus synthetic usage.

    Exists only to exercise the plumbing offline. Its bundle is labelled `DRY_RUN`.
    """

    def __init__(self, cases: tuple[ReleaseGoldenCase, ...]) -> None:
        self._by_coordinate = {
            (case.rule_id, case.case.perspective.value): case.case for case in cases
        }
        self.call_count = 0

    def converse(self, **kwargs: object) -> Mapping[str, object]:
        self.call_count += 1
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        case = self._by_coordinate[(body["rule"]["rule_id"], body["perspective"])]
        text = json.dumps(
            {
                "status": case.expected_status.value,
                "score": case.expected_score_min,
                "rationale": "dry run",
                "evidence_references": list(case.expected_evidence_references),
            }
        )
        return {
            "output": {"message": {"content": [{"text": text}]}},
            "usage": {"inputTokens": 1, "outputTokens": 1},
            "metrics": {"latencyMs": 1},
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--output", type=Path, required=True, help="Private bundle path.")
    parser.add_argument("--repetitions", type=int, default=REQUIRED_REPETITIONS)
    parser.add_argument("--platform-commit", help="40-hex platform commit (default: git HEAD).")
    parser.add_argument(
        "--customer-sandbox",
        action="store_true",
        help="Run against the protected customer runtime (real Bedrock + S3 artifacts).",
    )
    parser.add_argument("--snapshots", type=Path, help="Dry run: directory of {artifact_id}.json.")
    parser.add_argument("--customer-id", help="Sandbox: customer whose artifacts hold snapshots.")
    parser.add_argument("--artifact-bucket", help="Sandbox: content-addressed artifact bucket.")
    parser.add_argument(
        "--snapshot-index",
        type=Path,
        help="Sandbox: private JSON mapping artifact_id -> sha256:<hex>.",
    )
    parser.add_argument("--demo-commit-sha", help="D: demo repository merge commit (40 hex).")
    parser.add_argument("--deployment-id", help="D: platform deployment ID.")
    parser.add_argument(
        "--artifact-sha256",
        action="append",
        default=[],
        help="D: SHA-256 of one artifact the apply consumed (repeatable).",
    )
    return parser.parse_args(argv)


def _platform_commit(explicit: str | None) -> str:
    if explicit:
        return explicit
    from_env = os.environ.get("GITHUB_SHA")
    if from_env:
        return from_env
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            cwd=ROOT,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return _PLACEHOLDER_COMMIT


def _snapshot_reader(args: argparse.Namespace, *, boto3: object) -> GoldenSnapshotReader:
    if not args.customer_sandbox:
        if args.snapshots is None:
            raise GoldenObservationError("dry run requires --snapshots DIR")
        return DirectoryGoldenSnapshotReader(args.snapshots)
    missing = [
        name
        for name, value in (
            ("--customer-id", args.customer_id),
            ("--artifact-bucket", args.artifact_bucket),
            ("--snapshot-index", args.snapshot_index),
        )
        if not value
    ]
    if missing:
        raise GoldenObservationError(f"--customer-sandbox requires {', '.join(missing)}")
    from apps.backend.repositories.ports import ArtifactReference
    from apps.backend.repositories.s3 import S3ArtifactStore

    index = json.loads(args.snapshot_index.read_text(encoding="utf-8"))
    store = S3ArtifactStore(
        boto3.client("s3"),  # type: ignore[attr-defined]
        bucket_name=args.artifact_bucket,
        customer_id=args.customer_id,
    )
    return ArtifactStoreGoldenSnapshotReader(
        store,
        customer_id=args.customer_id,
        index=index,
        reference_factory=ArtifactReference,
    )


def _identity(args: argparse.Namespace, *, scenario_id: str) -> GoldenExecutionIdentity:
    if args.customer_sandbox:
        missing = [
            name
            for name, value in (
                ("--demo-commit-sha", args.demo_commit_sha),
                ("--deployment-id", args.deployment_id),
                ("--artifact-sha256", args.artifact_sha256),
            )
            if not value
        ]
        if missing:
            raise GoldenObservationError(f"--customer-sandbox requires {', '.join(missing)}")
        binding = derive_release_binding(
            commit_sha=args.demo_commit_sha,
            deployment_id=args.deployment_id,
            artifact_sha256s=args.artifact_sha256,
        )
        runtime_mode = CUSTOMER_SANDBOX_RUNTIME_MODE
    else:
        binding = derive_release_binding(
            commit_sha=args.demo_commit_sha or _PLACEHOLDER_COMMIT,
            deployment_id=args.deployment_id or "dry-run",
            artifact_sha256s=args.artifact_sha256 or [_PLACEHOLDER_ARTIFACT],
        )
        runtime_mode = DRY_RUN_RUNTIME_MODE
    return GoldenExecutionIdentity(
        scenario_id=scenario_id,
        runtime_mode=runtime_mode,
        platform_commit_sha=_platform_commit(args.platform_commit),
        repository_commit_sha256=binding.repository_commit_sha256,
        deployment_id_sha256=binding.deployment_id_sha256,
        artifact_set_sha256=binding.artifact_set_sha256,
        execution_id=new_execution_id(),
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        manifest = load_demo_policy_coverage(MANIFEST)
        registry = load_rule_registry(RULES)
        validate_demo_policy_coverage(
            manifest,
            registry=registry,
            initial_cases_path=INITIAL_CASES,
            verification_cases_path=CASES,
        )
        cases = load_release_golden_cases(CASES, manifest=manifest)
        profile = load_approved_model_profile(PROFILE)
        identity = _identity(args, scenario_id=manifest.scenario_id)
        if args.customer_sandbox:
            import boto3

            client: object = boto3.client("bedrock-runtime", region_name=profile.region)
        else:
            boto3 = None
            client = ExpectedOutcomeClient(cases)
        exporter = GoldenObservationExporter(
            client=client,
            profile=profile,
            cases=cases,
            rules=registry.rules,
            policy_profile=(manifest.policy_profile_id, manifest.policy_profile_version),
            snapshots=_snapshot_reader(args, boto3=boto3),
            repetitions=args.repetitions,
        )
        bundle = exporter.export(identity, generated_at=datetime.now(UTC))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(bundle, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
        )
        if args.customer_sandbox:
            # The producer checks its own output against the consumer's strict parser so a
            # schema drift is caught here, not at release time.
            load_golden_observation_bundle(args.output)
    except (
        GoldenObservationError,
        GoldenReleaseQualityError,
        ReleaseBindingError,
        ValueError,
        OSError,
    ) as error:
        print(f"Golden observation export: INVALID: {error}", file=sys.stderr)
        return 2

    observations = bundle["observations"]
    assert isinstance(observations, list)
    errors = sum(1 for item in observations if item["error_code"] is not None)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "runtime_mode": identity.runtime_mode,
                "execution_id": identity.execution_id,
                "observation_count": len(observations),
                "bedrock_call_count": exporter.bedrock_call_count,
                "execution_errors": errors,
            },
            sort_keys=True,
        )
    )
    if not args.customer_sandbox:
        print(
            "DRY_RUN bundle: the release gate rejects it by design; run with --customer-sandbox "
            "inside the protected customer runtime to produce evidence.",
            file=sys.stderr,
        )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
