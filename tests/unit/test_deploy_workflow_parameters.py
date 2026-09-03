"""Every settable CloudFormation parameter must be reachable from the deploy workflow.

A parameter that the template declares but the workflow never passes is unsettable: the stack
keeps its `Default`, and a feature that reads it stays fail-closed forever with no way to turn
it on. That is how `DeploymentRuntimeJson`, `DeploymentGitHubSecretArns`, and
`PolicyAuthoringModelProfileJson` sat at `""` while three merged features (PR write, apply-target
commit resolution, policy candidate extraction) were documented as "wired" (2026-09-03).

The check is a set comparison, so adding a parameter to the template without a way to supply it
fails here rather than during a live demo. Parameters the workflow computes itself are listed
explicitly below, with the reason they are not operator inputs.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "infrastructure" / "cloudformation" / "m0-foundation.yaml"
WORKFLOW = ROOT / ".github" / "workflows" / "deploy-m0-foundation.yml"

#: 워크플로가 직접 계산해 넘기는 값. 운영자가 고를 수 있는 입력이 아니다.
_DERIVED_PARAMETERS = {
    "ProjectName",  # workflow_dispatch input
    "Environment",  # workflow_dispatch input (stack_environment)
    "LambdaCodeS3Bucket",  # workflow_dispatch input
    "LambdaCodeS3Key",  # prepare-artifact job output
    "LambdaCodeS3ObjectVersion",  # prepare-artifact job output
    "LangGraphLayerS3Key",  # prepare-artifact job output
    "LangGraphLayerS3ObjectVersion",  # prepare-artifact job output
    "AssessmentScopeJson",  # workflow_dispatch input
}


def _template_parameters() -> set[str]:
    text = TEMPLATE.read_text(encoding="utf-8")
    block = text.split("\nParameters:", 1)[1].split("\nRules:", 1)[0]
    return set(re.findall(r"^  ([A-Za-z0-9]+):$", block, re.M))


def _workflow_parameter_overrides() -> set[str]:
    text = WORKFLOW.read_text(encoding="utf-8")
    return set(re.findall(r'"([A-Za-z0-9]+)=\$', text))


class DeployWorkflowParameterCoverageTest(unittest.TestCase):
    def test_every_template_parameter_is_passed_by_the_deploy_workflow(self) -> None:
        missing = _template_parameters() - _workflow_parameter_overrides()
        self.assertEqual(
            missing,
            set(),
            "these parameters are declared but unsettable: the stack keeps their Default "
            f"and any feature reading them stays fail-closed — {sorted(missing)}",
        )

    def test_derived_parameters_are_still_passed(self) -> None:
        """계산된 값도 override로 나가야 한다 — 목록이 낡으면 이 테스트가 먼저 깨진다."""
        self.assertTrue(_DERIVED_PARAMETERS <= _workflow_parameter_overrides())

    def test_the_deployment_pair_is_validated_before_cloudformation(self) -> None:
        """둘 중 하나만 설정한 배포는 CloudFormation Rule 전에 이름을 밝히고 멈춘다."""
        text = WORKFLOW.read_text(encoding="utf-8")
        for guard in (
            "DEPLOYMENT_RUNTIME_JSON is required alongside",
            "DEPLOYMENT_GITHUB_SECRET_ARNS is required alongside",
        ):
            with self.subTest(guard=guard):
                # 실패 메시지에 워크플로 전문을 싣지 않는다.
                self.assertTrue(guard in text, f"deploy workflow does not guard: {guard}")

    def test_credentials_come_from_oidc_not_static_keys(self) -> None:
        """배포는 OIDC로 role을 assume한다. 장기 access key는 어디에도 없다."""
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("id-token: write", text)
        self.assertIn("role-to-assume:", text)
        for forbidden in (
            "aws-access-key-id",
            "aws-secret-access-key",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertTrue(
                    forbidden not in text, f"deploy workflow references a static key: {forbidden}"
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
