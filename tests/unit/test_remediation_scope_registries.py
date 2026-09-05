"""The remediation scope the API decides with spans every registry the runtime publishes.

기준선 Rule 15개의 FAIL Finding이 전부 `RULE_NOT_IN_SCOPE`로 닫혔다 — 판정이 legacy Registry의
`remediation.json`만 읽었기 때문이다. 허용 범위는 Registry마다 커밋되므로 판정은 둘을 합쳐 본다.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from apps.backend.policy import (
    RemediationPolicyError,
    load_remediation_policy,
    load_rule_registry,
)
from packages.contracts.remediation_policy import RemediationEligibility

REPO_ROOT = Path(__file__).parents[2]
LEGACY = REPO_ROOT / "fixtures" / "rules"
BASELINE = REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023"


class MergedRemediationScopeTest(unittest.TestCase):
    def test_the_merged_policy_knows_rules_from_both_registries(self) -> None:
        policy = load_remediation_policy(LEGACY, BASELINE)

        self.assertIs(
            policy.eligibility(rule_id="S3-PUBLIC-001", version="2026-08-31"),
            RemediationEligibility.AUTOMATIC,
        )
        baseline_rule = next(
            r
            for r in load_rule_registry(BASELINE).rules
            if r.rule_id == "ISMSP-S3_BLOCK_PUBLIC_ACCESS"
        )
        self.assertIs(
            policy.eligibility(rule_id=baseline_rule.rule_id, version=baseline_rule.version),
            RemediationEligibility.AUTOMATIC,
        )
        self.assertEqual(
            len(policy.scopes),
            len(load_rule_registry(LEGACY).remediation.scopes)
            + len(load_rule_registry(BASELINE).remediation.scopes),
        )

    def test_a_rule_id_shared_by_two_registries_is_refused(self) -> None:
        """어느 판단이 이기는지 말할 수 없다. 조용히 한쪽을 고르지 않는다."""
        with TemporaryDirectory() as name:
            copy = Path(name)
            for path in LEGACY.glob("*.json"):
                (copy / path.name).write_bytes(path.read_bytes())
            with self.assertRaisesRegex(RemediationPolicyError, "duplicate remediation scope"):
                load_remediation_policy(LEGACY, copy)

    def test_at_least_one_registry_is_required(self) -> None:
        with self.assertRaises(ValueError):
            load_remediation_policy()

    def test_every_automated_baseline_rule_inherits_its_control_eligibility(self) -> None:
        """같은 통제, 같은 판단(ADR-0017). legacy에서 AUTOMATIC인 통제만 기준선에서도 AUTOMATIC이다."""
        from apps.backend.policy.control_catalog import LEGACY_RULE_CONTROL_KEYS

        legacy = load_rule_registry(LEGACY)
        by_control = {
            LEGACY_RULE_CONTROL_KEYS[scope.rule_id]: scope.eligibility
            for scope in legacy.remediation.scopes
            if scope.rule_id in LEGACY_RULE_CONTROL_KEYS
        }
        baseline = load_rule_registry(BASELINE)
        automated = [r for r in baseline.rules if r.control_key in by_control]
        self.assertEqual(len(automated), 15)
        for rule in automated:
            self.assertIs(
                baseline.remediation.eligibility(rule_id=rule.rule_id, version=rule.version),
                by_control[rule.control_key or ""],
                rule.rule_id,
            )
        automatic = sorted(
            r.control_key or ""
            for r in automated
            if baseline.remediation.eligibility(rule_id=r.rule_id, version=r.version)
            is RemediationEligibility.AUTOMATIC
        )
        self.assertEqual(
            automatic,
            ["RDS_NOT_PUBLIC", "S3_BLOCK_PUBLIC_ACCESS", "S3_BUCKET_ACL_DISABLED", "S3_TLS_ONLY"],
        )

    def test_the_committed_baseline_scope_is_the_generated_one(self) -> None:
        scopes = json.loads((BASELINE / "remediation.json").read_text(encoding="utf-8"))
        self.assertEqual(len(scopes), 15)
        self.assertTrue(all(s["rule_id"].startswith("ISMSP-") for s in scopes))


if __name__ == "__main__":
    unittest.main()
