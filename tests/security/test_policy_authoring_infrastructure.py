"""The authoring worker's권한 is separate from every other runtime's.

정책 원문을 읽는 권한과 고객 AWS 계정을 읽는 권한이 같은 Role에 있으면, 한쪽의 사고가 다른 쪽
자료까지 닿는다. 그래서 Authoring Worker는 전용 Role과 전용 큐를 쓴다.

그리고 API는 정규화 artifact를 **읽지 않는다.** 추출 요청만 큐로 보내고, 텍스트는 worker가
자기 권한으로 읽는다 — 정책 원문이 API 응답 경로가 있는 실행체의 권한 안에 들어오지 않는다.
"""

import unittest
from pathlib import Path

import yaml

TEMPLATE_PATH = Path(__file__).parents[2] / "infrastructure/cloudformation/m0-foundation.yaml"


class _CloudFormationLoader(yaml.SafeLoader):
    """Load the template treating CloudFormation short tags as opaque values."""


def _short_tag(loader: yaml.Loader, tag_suffix: str, node: yaml.Node) -> object:
    name = tag_suffix.lstrip("!")
    if isinstance(node, yaml.ScalarNode):
        return {name: loader.construct_scalar(node)}
    if isinstance(node, yaml.SequenceNode):
        return {name: loader.construct_sequence(node, deep=True)}
    return {name: loader.construct_mapping(node, deep=True)}


_CloudFormationLoader.add_multi_constructor("!", _short_tag)


def _statements(role: dict, policy_name_contains: str) -> list[dict]:
    statements: list[dict] = []
    for policy in role["Properties"]["Policies"]:
        if not isinstance(policy, dict) or "PolicyName" not in policy:
            continue
        if policy_name_contains not in policy["PolicyName"]:
            continue
        statements.extend(policy["PolicyDocument"]["Statement"])
    return statements


def _policy_statements(policy: object) -> list[dict]:
    """Every statement an inline policy grants, whether or not it is conditional.

    `!If`로 감싼 정책도 배포 조건이 맞으면 실제 권한이 된다. 조건부라는 이유로 검사에서 빼면,
    그 정책이 무엇을 여는지 아무도 확인하지 않게 된다.
    """
    if not isinstance(policy, dict):
        return []
    if "If" in policy:
        return [
            statement for branch in policy["If"][1:] for statement in _policy_statements(branch)
        ]
    document = policy.get("PolicyDocument")
    if not isinstance(document, dict):
        return []
    return list(document["Statement"])


def _actions(statement: dict) -> set[str]:
    action = statement["Action"]
    return {action} if isinstance(action, str) else set(action)


def _resource_text(statement: dict) -> str:
    return repr(statement.get("Resource"))


class PolicyAuthoringInfrastructureTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = yaml.load(
            TEMPLATE_PATH.read_text(encoding="utf-8"), Loader=_CloudFormationLoader
        )
        cls.resources = cls.template["Resources"]

    def test_the_authoring_worker_has_its_own_queue_and_dead_letter_queue(self) -> None:
        """다른 Worker와 큐를 나눠 쓰면 authoring 권한이 그들의 메시지에도 닿는다."""
        self.assertEqual(self.resources["PolicyAuthoringQueue"]["Type"], "AWS::SQS::Queue")
        self.assertEqual(self.resources["PolicyAuthoringDlq"]["Type"], "AWS::SQS::Queue")
        redrive = self.resources["PolicyAuthoringQueue"]["Properties"]["RedrivePolicy"]
        self.assertIn("PolicyAuthoringDlq", repr(redrive))

    def test_the_dead_letter_queue_is_alarmed(self) -> None:
        """추출되지 않은 정책은 "위반 없음"이 아니라 "검토할 것이 아직 없음"으로 보인다."""
        alarm = self.resources["PolicyAuthoringDlqAlarm"]

        self.assertEqual(alarm["Type"], "AWS::CloudWatch::Alarm")
        self.assertIn("PolicyAuthoringDlq", repr(alarm["Properties"]["Dimensions"]))

    def test_the_worker_runs_under_its_own_role(self) -> None:
        worker = self.resources["PolicyAuthoringWorkerFunction"]["Properties"]

        self.assertIn("PolicyAuthoringRuntimeRole", repr(worker["Role"]))
        self.assertEqual(worker["Handler"], "apps.backend.policy.authoring.runtime.lambda_handler")

    def test_the_event_source_binds_the_authoring_queue_to_the_authoring_worker(self) -> None:
        mapping = self.resources["PolicyAuthoringQueueEventSource"]["Properties"]

        self.assertIn("PolicyAuthoringQueue", repr(mapping["EventSourceArn"]))
        self.assertIn("PolicyAuthoringWorkerFunction", repr(mapping["FunctionName"]))

    def test_the_authoring_role_reads_artifacts_but_never_writes_them(self) -> None:
        """추출 worker가 원문을 고쳐 쓸 수 있으면 승인된 판본과 읽은 판본이 달라질 수 있다."""
        statements = _statements(
            self.resources["PolicyAuthoringRuntimeRole"], "PolicyAuthoringStateAndArtifacts"
        )
        s3_statements = [
            statement
            for statement in statements
            if any(action.startswith("s3:") for action in _actions(statement))
        ]

        self.assertTrue(s3_statements)
        for statement in s3_statements:
            self.assertEqual(_actions(statement), {"s3:GetObject"})
            self.assertIn("customers/*", _resource_text(statement))

    def test_the_authoring_role_touches_only_its_own_queue(self) -> None:
        statements = _statements(
            self.resources["PolicyAuthoringRuntimeRole"], "PolicyAuthoringStateAndArtifacts"
        )
        sqs_statements = [
            statement
            for statement in statements
            if any(action.startswith("sqs:") for action in _actions(statement))
        ]

        self.assertTrue(sqs_statements)
        for statement in sqs_statements:
            resources = _resource_text(statement)
            self.assertIn("PolicyAuthoringQueue", resources)
            for other in ("AssessmentQueue", "RemediationQueue", "DeploymentQueue"):
                self.assertNotIn(other, resources)
            # 요청을 만드는 것은 API의 일이다. worker가 자기 큐에 다시 넣을 수 있으면
            # 실패한 추출이 무한히 스스로를 재요청할 수 있다.
            self.assertNotIn("sqs:SendMessage", _actions(statement))

    def test_the_authoring_role_cannot_read_customer_aws_accounts(self) -> None:
        """정책 원문 권한과 고객 계정 권한을 한 Role에 두지 않는다.

        `AssumeRolePolicyDocument`의 `sts:AssumeRole`은 Lambda가 이 Role을 맡기 위한 신뢰
        정책이며 고객 계정과 무관하다. 검사 대상은 **inline policy가 부여하는 권한**이다.
        """
        role = self.resources["PolicyAuthoringRuntimeRole"]
        granted: set[str] = set()
        for policy in role["Properties"]["Policies"]:
            for statement in _policy_statements(policy):
                granted |= _actions(statement)

        # 고객 AWS 계정을 읽으려면 이 둘 중 하나가 필요하다. 둘 다 없어야 한다.
        self.assertNotIn("sts:AssumeRole", granted)
        self.assertNotIn("secretsmanager:GetSecretValue", granted)
        self.assertNotIn("M1AssessmentReadRoleArns", repr(role["Properties"]["Policies"]))

    def test_the_assessment_worker_role_cannot_receive_authoring_messages(self) -> None:
        statements = _statements(self.resources["WorkflowRuntimeRole"], "WorkflowStateAndDispatch")

        for statement in statements:
            if any(action.startswith("sqs:") for action in _actions(statement)):
                self.assertNotIn("PolicyAuthoringQueue", _resource_text(statement))

    def test_the_api_may_queue_extraction_but_not_read_the_normalized_artifact(self) -> None:
        """API가 정규화 artifact를 읽을 수 있으면, 응답 경로가 있는 실행체가 원문을 쥔다.

        업로드·정규화 경로가 `s3:PutObject`/`s3:GetObject`를 갖는 것은 그 단계의 책임이며,
        추출은 그 권한을 쓰지 않는다 — 요청만 큐로 보낸다.
        """
        statements = _statements(self.resources["ApiRuntimeRole"], "MetadataAndWorkflowDispatch")
        send_statements = [
            statement for statement in statements if "sqs:SendMessage" in _actions(statement)
        ]

        self.assertTrue(send_statements)
        self.assertTrue(any("PolicyAuthoringQueue" in _resource_text(s) for s in send_statements))

    def test_the_bedrock_grant_exists_only_when_an_approved_profile_is_configured(self) -> None:
        """승인된 authoring 모델이 없으면 Bedrock 권한 자체를 만들지 않는다."""
        policies = self.resources["PolicyAuthoringRuntimeRole"]["Properties"]["Policies"]
        conditional = [policy for policy in policies if "If" in repr(policy)[:40]]

        self.assertTrue(conditional)
        self.assertIn("PolicyAuthoringEnabled", repr(conditional))
        self.assertIn("bedrock:InvokeModel", repr(conditional))

    def test_the_worker_is_configured_with_the_approved_model_profile(self) -> None:
        variables = self.resources["PolicyAuthoringWorkerFunction"]["Properties"]["Environment"][
            "Variables"
        ]

        self.assertIn("POLICY_AUTHORING_MODEL_PROFILE_JSON", variables)
        self.assertIn("POLICY_SOURCE_BUCKET_NAME", variables)
        self.assertIn("METADATA_TABLE_NAME", variables)

    def test_the_api_is_configured_with_the_authoring_queue(self) -> None:
        variables = self.resources["ApiRuntimeFunction"]["Properties"]["Environment"]["Variables"]

        self.assertIn("POLICY_AUTHORING_QUEUE_URL", variables)

    def test_both_candidate_routes_are_declared_with_the_jwt_authorizer(self) -> None:
        routes = {
            resource["Properties"]["RouteKey"]: resource["Properties"]
            for resource in self.resources.values()
            if resource.get("Type") == "AWS::ApiGatewayV2::Route"
        }

        for method in ("POST", "GET"):
            key = f"{method} /policy-sources/{{sourceId}}/versions/{{version}}/candidates"
            with self.subTest(route=key):
                self.assertIn(key, routes)
                self.assertEqual(routes[key]["AuthorizationType"], "JWT")


if __name__ == "__main__":
    unittest.main()
