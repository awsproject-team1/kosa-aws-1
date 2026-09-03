"""The Golden runner drives the production evaluator, not a bench-only prompt."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import run_assessment_golden as golden  # noqa: E402

CASES = ROOT / "fixtures" / "m1" / "golden_dataset_cases.json"
PROFILE = ROOT / "fixtures" / "m1" / "assessment_model_profile.json"
RULES = ROOT / "fixtures" / "rules"


def _write_snapshots(directory: Path, cases) -> None:
    for case in cases.values():
        if case.perspective.value == "DRIFT":
            continue
        (directory / f"{case.resource_snapshot_artifact_id}.json").write_text(
            json.dumps(
                {
                    "resource_id": "bucket-public-001",
                    "resource_document": {"attributes": {"public_access_block": {}}},
                    "evidence_references": list(case.expected_evidence_references),
                }
            ),
            encoding="utf-8",
        )


class GoldenRunnerTest(unittest.TestCase):
    def test_dry_run_passes_the_gate_through_the_production_evaluator(self) -> None:
        cases = golden.load_cases(CASES)
        profile = golden.load_profile(PROFILE)
        with tempfile.TemporaryDirectory() as directory:
            snapshots = Path(directory)
            _write_snapshots(snapshots, cases)
            evaluator = golden.build_evaluator(
                cases=cases,
                profile=profile,
                rules_path=RULES,
                snapshots=snapshots,
                client=golden.DryRunClient(cases),
            )
            reports = golden.run(cases=cases, evaluator=evaluator, repetitions=2)
        # 6 Rule × IAC/AWS_ACTUAL = 12 model-evaluated cases; DRIFT is derived, not run.
        self.assertEqual(len(reports), 12)
        self.assertTrue(all(report["passes"] for report in reports))
        self.assertTrue(all(report["runs"] == 2 for report in reports))

    def test_missing_snapshot_is_an_error_not_an_empty_evaluation(self) -> None:
        cases = golden.load_cases(CASES)
        profile = golden.load_profile(PROFILE)
        with tempfile.TemporaryDirectory() as directory:
            evaluator = golden.build_evaluator(
                cases=cases,
                profile=profile,
                rules_path=RULES,
                snapshots=Path(directory),
                client=golden.DryRunClient(cases),
            )
            with self.assertRaisesRegex(golden.GoldenRunError, "missing"):
                golden.run(cases=cases, evaluator=evaluator, repetitions=2)

    def test_dry_run_cli_exits_zero(self) -> None:
        cases = golden.load_cases(CASES)
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory), cases)
            with contextlib.redirect_stdout(io.StringIO()):
                code = golden.main(
                    [
                        "--snapshots",
                        directory,
                        "--repetitions",
                        "2",
                        "--dry-run",
                        "--output",
                        str(Path(directory) / "report.json"),
                    ]
                )
            report = json.loads((Path(directory) / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertTrue(report["dry_run"])
        self.assertEqual(report["model_profile_id"], "assessment-nova-lite-m1-v3")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
