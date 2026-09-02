from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.backend.policy import load_rule_registry  # noqa: E402
from apps.backend.policy.demo import (  # noqa: E402
    DemoPolicyCoverageError,
    load_demo_policy_coverage,
    validate_demo_policy_coverage,
)


def main() -> int:
    try:
        report = validate_demo_policy_coverage(
            load_demo_policy_coverage(ROOT / "fixtures" / "m4" / "demo_policy_coverage.json"),
            registry=load_rule_registry(ROOT / "fixtures" / "rules"),
            initial_cases_path=ROOT / "fixtures" / "m1" / "golden_dataset_cases.json",
            verification_cases_path=(
                ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json"
            ),
        )
    except DemoPolicyCoverageError as error:
        print(f"M4 demo policy coverage: FAIL: {error}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "status": "PASS",
                "scenario_id": report.scenario_id,
                "profile_rule_count": report.profile_rule_count,
                "control_count": report.control_count,
                "policy_evidence_count": report.policy_evidence_count,
                "golden_case_count": report.golden_case_count,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
