from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.backend.assessment.release_quality import (  # noqa: E402
    REQUIRED_REPETITIONS,
    GoldenReleaseQualityError,
    evaluate_golden_release_quality,
    load_approved_model_profile,
    load_golden_observation_bundle,
    load_release_golden_cases,
    render_golden_release_markdown,
)
from apps.backend.policy import load_rule_registry  # noqa: E402
from apps.backend.policy.demo import (  # noqa: E402
    load_demo_policy_coverage,
    validate_demo_policy_coverage,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate M4 customer-sandbox Golden observations and emit sanitized evidence."
    )
    parser.add_argument(
        "--observations",
        type=Path,
        help="Local identifier-only CUSTOMER_SANDBOX observation bundle. Omit for dry-run.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "build" / "m4-release",
        help="Directory for sanitized JSON/Markdown reports (default: build/m4-release).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        manifest = load_demo_policy_coverage(ROOT / "fixtures" / "m4" / "demo_policy_coverage.json")
        validate_demo_policy_coverage(
            manifest,
            registry=load_rule_registry(ROOT / "fixtures" / "rules"),
            initial_cases_path=ROOT / "fixtures" / "m1" / "golden_dataset_cases.json",
            verification_cases_path=(
                ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json"
            ),
        )
        cases = load_release_golden_cases(
            ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json",
            manifest=manifest,
        )
        profile = load_approved_model_profile(
            ROOT / "fixtures" / "m1" / "assessment_model_profile.json"
        )
        if args.observations is None:
            bedrock_cases = sum(case.case.perspective.value != "DRIFT" for case in cases)
            print(
                json.dumps(
                    {
                        "status": "EXTERNAL_EVIDENCE_REQUIRED",
                        "scenario_id": manifest.scenario_id,
                        "model_profile_id": profile.model_profile_id,
                        "case_count": len(cases),
                        "repetitions": REQUIRED_REPETITIONS,
                        "planned_bedrock_calls": bedrock_cases * REQUIRED_REPETITIONS,
                        "planned_code_derived_results": (
                            (len(cases) - bedrock_cases) * REQUIRED_REPETITIONS
                        ),
                    },
                    sort_keys=True,
                )
            )
            print(
                "Dry-run only: provide --observations from the protected customer runtime; "
                "fixture output is not release evidence.",
                file=sys.stderr,
            )
            return 0

        report = evaluate_golden_release_quality(
            load_golden_observation_bundle(args.observations),
            manifest=manifest,
            cases=cases,
            approved_model_profile=profile,
        )
    except (GoldenReleaseQualityError, OSError) as error:
        print(f"M4 Golden release gate: INVALID: {error}", file=sys.stderr)
        return 2

    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "m4-golden-release-report.json"
    markdown_path = args.output_dir / "m4-golden-release-report.md"
    json_path.write_text(
        json.dumps(report.to_dict(), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_golden_release_markdown(report), encoding="utf-8")
    print(f"Sanitized JSON: {json_path}")
    print(f"Sanitized Markdown: {markdown_path}")
    print(f"M4 Golden release gate: {'PASS' if report.passes else 'FAIL'}")
    return 0 if report.passes else 1


if __name__ == "__main__":
    raise SystemExit(main())
