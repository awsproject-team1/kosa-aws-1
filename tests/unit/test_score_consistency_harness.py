"""The consistency harness measures; it must not hide variance, contract errors, or direction."""

import json
import unittest
from pathlib import Path

from scripts.measure_score_consistency import (
    GOLDEN_FAIL_SCORE_MAX,
    DryRunClient,
    attribute_order_invariance,
    builtin_cases,
    load_profile,
    measure,
    render_markdown,
    summarize,
)

ROOT = Path(__file__).parents[2]
PROFILE = load_profile(ROOT / "fixtures/m1/assessment_model_profile.json")
CASES = builtin_cases(ROOT / "fixtures/rules")


class _ScriptedClient:
    """Returns one scripted answer per call so statistics can be checked exactly."""

    def __init__(self, answers: list[dict[str, object]]) -> None:
        self._answers = answers

    def converse(self, **kwargs: object):
        body = json.loads(kwargs["messages"][0]["content"][0]["text"])  # type: ignore[index]
        answer = dict(self._answers.pop(0))
        answer.setdefault("evidence_references", body["allowed_evidence_references"][:1])
        answer.setdefault("rationale", "scripted")
        return {"output": {"message": {"content": [{"text": json.dumps(answer)}]}}}


def _case(case_id: str):
    return next(case for case in CASES if case.case_id == case_id)


class HarnessTest(unittest.TestCase):
    def test_builtin_cases_cover_the_four_primary_resources_and_both_perspectives(self) -> None:
        rules = {case.rule.rule_id for case in CASES}
        self.assertTrue(
            {"RDS-PUBLIC-001", "S3-PUBLIC-001", "ALB-HTTPS-001", "EC2-PUBLIC-IP-001"} <= rules
        )
        self.assertEqual({case.perspective.value for case in CASES}, {"IAC", "AWS_ACTUAL"})
        transitions = [case for case in CASES if case.transition_from]
        ids = {case.case_id for case in CASES}
        self.assertTrue(all(case.transition_from in ids for case in transitions))

    def test_partial_compliance_cases_exist_and_are_not_all_or_nothing(self) -> None:
        """Without a partially compliant document, a 0/100 distribution proves nothing.

        Live measurement showed the model answers 0 or 100 on these too, and that two of
        them are false negatives — which the all-or-nothing cases could not have surfaced.
        """
        partial = [case for case in CASES if case.kind == "partial-compliance"]
        self.assertEqual(len(partial), 4)
        flags = next(
            case for case in partial if case.case_id == "s3-three-of-four-actual"
        ).resource_document["attributes"]["public_access_block"]
        self.assertEqual(sum(bool(value) for value in flags.values()), 3)
        listeners = next(
            case for case in partial if case.case_id == "alb-https-plus-http-actual"
        ).resource_document["attributes"]["listeners"]
        self.assertEqual({entry["Protocol"] for entry in listeners}, {"HTTPS", "HTTP"})
        rds = next(
            case for case in partial if case.case_id == "rds-private-open-sg-actual"
        ).resource_document
        self.assertFalse(rds["attributes"]["db_instance"]["PubliclyAccessible"])
        self.assertEqual(
            rds["attributes"]["vpc_security_groups"][0]["IpPermissions"][0]["IpRanges"],
            [{"CidrIp": "0.0.0.0/0"}],
        )

    def test_dry_run_is_deterministic_without_jitter(self) -> None:
        reports = measure(
            (_case("rds-public-actual"),),
            client_factory=lambda: DryRunClient(),
            profile=PROFILE,
            repetitions=5,
        )
        summary = reports[0].summary()
        self.assertEqual(summary["range"], 0.0)
        self.assertEqual(summary["stdev"], 0.0)
        self.assertEqual(summary["status_agreement"], 1.0)
        self.assertEqual(summary["finding_agreement"], 1.0)
        self.assertEqual(summary["expected_status_accuracy"], 1.0)

    def test_variance_is_reported_not_rounded_away(self) -> None:
        answers = [{"status": "FAIL", "score": s} for s in (32, 67, 45, 81, 29)]
        client = _ScriptedClient(answers)
        reports = measure(
            (_case("rds-public-actual"),),
            client_factory=lambda: client,
            profile=PROFILE,
            repetitions=5,
        )
        summary = reports[0].summary()
        self.assertEqual(summary["scores"], [32.0, 67.0, 45.0, 81.0, 29.0])
        self.assertEqual(summary["min"], 29.0)
        self.assertEqual(summary["max"], 81.0)
        self.assertEqual(summary["range"], 52.0)
        self.assertEqual(summary["max_pairwise_diff"], 52.0)
        self.assertGreater(summary["stdev"], 20)
        # FAIL above the Golden violation ceiling is a severe-overestimation candidate.
        self.assertEqual(
            summary["severe_overestimation_candidates"],
            [s for s in (32.0, 67.0, 45.0, 81.0) if s > GOLDEN_FAIL_SCORE_MAX],
        )

    def test_status_disagreement_lowers_agreement_and_finding_agreement(self) -> None:
        answers = [
            {"status": "FAIL", "score": 10},
            {"status": "PASS", "score": 90},
            {"status": "FAIL", "score": 12},
        ]
        client = _ScriptedClient(answers)
        summary = measure(
            (_case("rds-public-actual"),),
            client_factory=lambda: client,
            profile=PROFILE,
            repetitions=3,
        )[0].summary()
        self.assertAlmostEqual(summary["status_agreement"], 2 / 3, places=3)
        self.assertAlmostEqual(summary["finding_agreement"], 2 / 3, places=3)
        self.assertAlmostEqual(summary["expected_status_accuracy"], 2 / 3, places=3)

    def test_contract_errors_are_recorded_per_run_and_the_run_continues(self) -> None:
        answers = [
            {"status": "FAIL", "score": 150},  # out of range → contract error
            {"status": "FAIL", "score": 11},
        ]
        client = _ScriptedClient(answers)
        summary = measure(
            (_case("rds-public-actual"),),
            client_factory=lambda: client,
            profile=PROFILE,
            repetitions=2,
        )[0].summary()
        self.assertEqual(len(summary["contract_errors"]), 1)
        self.assertIn("score must be a number", summary["contract_errors"][0])
        self.assertEqual(summary["scores"], [11.0])

    def test_non_judgment_scores_are_normalized_so_no_contradiction_can_be_recorded(self) -> None:
        """The runtime pins MANUAL_REVIEW/INSUFFICIENT_EVIDENCE to 0; the harness sees 0."""
        client = _ScriptedClient([{"status": "INSUFFICIENT_EVIDENCE", "score": 55}])
        summary = measure(
            (_case("rds-public-actual"),),
            client_factory=lambda: client,
            profile=PROFILE,
            repetitions=1,
        )[0].summary()
        self.assertEqual(summary["scores"], [0.0])
        self.assertEqual(summary["non_judgment_score_contradictions"], [])

    def test_expected_transition_direction_is_reported(self) -> None:
        pair = (_case("rds-public-actual"), _case("rds-private-actual"))
        client = _ScriptedClient([{"status": "FAIL", "score": 20}, {"status": "PASS", "score": 88}])
        reports = measure(pair, client_factory=lambda: client, profile=PROFILE, repetitions=1)
        summary = summarize(
            profile=PROFILE, repetitions=1, dry_run=True, cases=pair, reports=reports
        )
        self.assertEqual(summary["transitions"][0]["direction_ok"], True)
        regressed = _ScriptedClient(
            [{"status": "FAIL", "score": 60}, {"status": "PASS", "score": 40}]
        )
        reports = measure(pair, client_factory=lambda: regressed, profile=PROFILE, repetitions=1)
        summary = summarize(
            profile=PROFILE, repetitions=1, dry_run=True, cases=pair, reports=reports
        )
        self.assertEqual(summary["transitions"][0]["direction_ok"], False)

    def test_attribute_order_cannot_change_the_prompt(self) -> None:
        for case in CASES:
            if case.kind == "self-agreement":
                with self.subTest(case=case.case_id):
                    self.assertTrue(attribute_order_invariance(case)["prompt_bytes_identical"])

    def test_markdown_report_lists_every_case(self) -> None:
        reports = measure(
            CASES[:2], client_factory=lambda: DryRunClient(), profile=PROFILE, repetitions=2
        )
        summary = summarize(
            profile=PROFILE, repetitions=2, dry_run=True, cases=CASES[:2], reports=reports
        )
        text = render_markdown(summary)
        for case in CASES[:2]:
            self.assertIn(case.case_id, text)
        self.assertIn("Expected transitions", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
