"""The CloudFormation execution role must be allowed to provision every foundation resource.

The customer bootstrap grants the execution role a fixed set of service prefixes. The foundation
template grows independently. When a new resource type lands in the foundation without its
service in the bootstrap, the stack update fails with AccessDenied on that one resource and
CloudFormation rolls back the whole change set — which is how `PolicyAuthoringDlqAlarm`
(the first `AWS::CloudWatch::Alarm`) blocked deploy run 33766208595 on 2026-09-03.

This test maps each foundation resource type to the IAM service prefix that creates it and
checks that both bootstrap variants allow at least one action under that prefix. It fails in
CI, before a protected deployment is dispatched and approved.
"""

import re
import unittest
from pathlib import Path

import yaml
from yaml.nodes import MappingNode, ScalarNode, SequenceNode

CLOUDFORMATION = Path(__file__).parents[2] / "infrastructure" / "cloudformation"
FOUNDATION = CLOUDFORMATION / "m0-foundation.yaml"
BOOTSTRAPS = (
    CLOUDFORMATION / "m1-customer-bootstrap.yaml",
    CLOUDFORMATION / "m1-customer-bootstrap-roles.yaml",
)

#: CloudFormation resource namespace → IAM service prefix whose actions create it. A namespace
#: missing here fails the test on purpose: the mapping must be extended consciously.
_SERVICE_PREFIX = {
    "ApiGatewayV2": "apigateway",
    "CloudTrail": "cloudtrail",
    "CloudWatch": "cloudwatch",
    "Cognito": "cognito-idp",
    "DynamoDB": "dynamodb",
    "Events": "events",
    "IAM": "iam",
    "Lambda": "lambda",
    "Logs": "logs",
    "S3": "s3",
    "SQS": "sqs",
}
_RESOURCE_TYPE = re.compile(r"^AWS::([A-Za-z0-9]+)::[A-Za-z0-9]+$")


def _construct_intrinsic(loader: yaml.SafeLoader, _suffix: str, node: yaml.Node) -> object:
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, MappingNode):
        return loader.construct_mapping(node)
    raise TypeError(f"Unsupported CloudFormation node: {type(node).__name__}")


yaml.SafeLoader.add_multi_constructor("!", _construct_intrinsic)


def _template(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"{path.name} must be a mapping")
    return loaded


def _foundation_service_prefixes() -> dict[str, set[str]]:
    """Return {service prefix: resource types} for every resource the foundation declares."""
    prefixes: dict[str, set[str]] = {}
    for logical_id, resource in _template(FOUNDATION)["Resources"].items():
        resource_type = resource["Type"]
        match = _RESOURCE_TYPE.match(resource_type)
        if match is None:
            raise AssertionError(f"{logical_id} has an unexpected Type {resource_type!r}")
        namespace = match.group(1)
        if namespace not in _SERVICE_PREFIX:
            raise AssertionError(
                f"{logical_id} ({resource_type}) uses namespace {namespace!r}; add its IAM "
                "service prefix to _SERVICE_PREFIX so the bootstrap grant can be checked"
            )
        prefixes.setdefault(_SERVICE_PREFIX[namespace], set()).add(resource_type)
    return prefixes


def _allowed_action_prefixes(bootstrap: Path) -> set[str]:
    role = _template(bootstrap)["Resources"]["FoundationExecutionRole"]
    allowed: set[str] = set()
    for policy in role["Properties"]["Policies"]:
        for statement in policy["PolicyDocument"]["Statement"]:
            if statement.get("Effect") != "Allow":
                continue
            actions = statement["Action"]
            for action in [actions] if isinstance(actions, str) else actions:
                allowed.add(action.split(":", 1)[0])
    return allowed


class BootstrapExecutionRoleCoversFoundationTest(unittest.TestCase):
    def test_every_foundation_service_is_granted_to_the_execution_role(self) -> None:
        needed = _foundation_service_prefixes()
        for bootstrap in BOOTSTRAPS:
            with self.subTest(bootstrap=bootstrap.name):
                allowed = _allowed_action_prefixes(bootstrap)
                missing = {
                    prefix: sorted(types)
                    for prefix, types in needed.items()
                    if prefix not in allowed
                }
                self.assertEqual(
                    missing,
                    {},
                    "the execution role cannot create these foundation resources; the stack "
                    f"update would fail with AccessDenied and roll back — {missing}",
                )

    def test_alarm_management_is_scoped_to_the_three_actions_cloudformation_needs(self) -> None:
        """`cloudwatch:*` would also cover dashboards, log-based metrics and more; keep it narrow."""
        for bootstrap in BOOTSTRAPS:
            with self.subTest(bootstrap=bootstrap.name):
                role = _template(bootstrap)["Resources"]["FoundationExecutionRole"]
                cloudwatch_actions = {
                    action
                    for policy in role["Properties"]["Policies"]
                    for statement in policy["PolicyDocument"]["Statement"]
                    for action in (
                        [statement["Action"]]
                        if isinstance(statement["Action"], str)
                        else statement["Action"]
                    )
                    if action.startswith("cloudwatch:")
                }
                self.assertEqual(
                    cloudwatch_actions,
                    {
                        "cloudwatch:DeleteAlarms",
                        "cloudwatch:DescribeAlarms",
                        "cloudwatch:PutMetricAlarm",
                    },
                )

    def test_bootstrap_variants_grant_the_same_execution_role_services(self) -> None:
        """Both variants deploy the same foundation; they must not drift from each other."""
        first, second = (_allowed_action_prefixes(path) for path in BOOTSTRAPS)
        self.assertEqual(first, second)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
