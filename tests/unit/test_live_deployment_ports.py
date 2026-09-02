"""M3 D live 실행 port 어댑터 Unit 테스트 (ADR-0019).

실제 GitHub/AWS 호출은 주입한 fake로 대체한다. 고정하는 것:
- 세 어댑터가 각 정본 port Protocol을 만족한다.
- ApplyDispatchPort는 workflow_dispatch만 호출하고 input이 deployment_id/commit_sha/plan_hash다.
  dispatch는 run_id를 주지 않고 ApplyDispatchReceipt(workflow_path)만 돌려준다.
- WorkflowRunReader는 WorkflowRunReference(실제 run_id)로 재조회하고, 실패(404·형식·미완료·
  plan_hash 마커 부재)는 예외가 아니라 FAILURE 결론 값이다(EventBridge 불신뢰, section 7).
- ActualRereadPort는 read-only Resource Tool 재사용이며 반환값이 없다.
- 모든 어댑터가 단일 scope 밖 요청을 거부한다.
"""

import json
import unittest

from agent.runtime import (
    ActualRereadPort,
    ApplyDispatchPort,
    AwsResourceView,
    LiveActualRereadPort,
    LiveApplyDispatchPort,
    LiveDeploymentPortError,
    LiveWorkflowRunReader,
    WorkflowRunReader,
)
from agent.runtime.aws_resource_tool import AwsResourceTool
from packages.contracts import (
    ApplyDispatchReceipt,
    ArtifactReference,
    ArtifactType,
    AwsResourceQuery,
    DeploymentApproval,
    TerraformPlan,
    TerraformStateVersion,
    WorkflowConclusion,
    WorkflowRunFacts,
    WorkflowRunReference,
)
from packages.contracts.remediation import RemediationSyncTarget

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
REPO_FULL = "acme/iac"
AWS_ACCOUNT_ID = "111122223333"
DEPLOYMENT_ID = "dep-abc123"
PLAN_HASH = "f" * 64
COMMIT = "a" * 40
LINEAGE = "11111111-2222-3333-4444-555555555555"
APPLY_WORKFLOW = ".github/workflows/terraform-apply.yml"


def build_plan() -> TerraformPlan:
    return TerraformPlan(
        deployment_id=DEPLOYMENT_ID,
        commit_sha=COMMIT,
        plan_hash=PLAN_HASH,
        artifact=ArtifactReference(
            artifact_id="art-plan-1",
            artifact_type=ArtifactType.TERRAFORM_PLAN,
            content_sha256=PLAN_HASH,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
    )


def build_approval() -> DeploymentApproval:
    return DeploymentApproval(
        deployment_id=DEPLOYMENT_ID, approved_by="admin-1", commit_sha=COMMIT, plan_hash=PLAN_HASH
    )


class ApplyDispatchTest(unittest.TestCase):
    def _capture(self):
        calls: list[tuple[str, dict[str, str], bytes]] = []

        def dispatch(url, headers, body):
            calls.append((url, dict(headers), body))

        return calls, dispatch

    def _port(self, dispatch):
        return LiveApplyDispatchPort(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            dispatch=dispatch,
        )

    def test_protocol_conformance(self) -> None:
        _, dispatch = self._capture()
        self.assertIsInstance(self._port(dispatch), ApplyDispatchPort)

    def test_dispatch_posts_workflow_dispatch_and_returns_receipt(self) -> None:
        calls, dispatch = self._capture()
        receipt = self._port(dispatch).dispatch_apply(
            approval=build_approval(),
            plan=build_plan(),
            state_version=TerraformStateVersion(lineage=LINEAGE, serial=7),
        )
        self.assertIsInstance(receipt, ApplyDispatchReceipt)
        self.assertEqual(receipt.deployment_id, DEPLOYMENT_ID)
        self.assertEqual(receipt.workflow_path, APPLY_WORKFLOW)
        self.assertEqual(len(calls), 1)
        url, headers, body = calls[0]
        self.assertIn("/actions/workflows/terraform-apply.yml/dispatches", url)
        self.assertEqual(headers["Authorization"], "Bearer tok")
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["ref"], COMMIT)
        self.assertEqual(
            payload["inputs"],
            {"deployment_id": DEPLOYMENT_ID, "commit_sha": COMMIT, "plan_hash": PLAN_HASH},
        )

    def test_dispatch_rejects_unbound_approval(self) -> None:
        _, dispatch = self._capture()
        wrong = DeploymentApproval(
            deployment_id=DEPLOYMENT_ID, approved_by="a", commit_sha=COMMIT, plan_hash="0" * 64
        )
        with self.assertRaises(LiveDeploymentPortError):
            self._port(dispatch).dispatch_apply(
                approval=wrong,
                plan=build_plan(),
                state_version=TerraformStateVersion(lineage=LINEAGE, serial=7),
            )


class WorkflowRunReaderTest(unittest.TestCase):
    def _reader(self, payload=None, fail=False):
        def request(url, headers):
            if fail:
                from agent.runtime.live_deployment_ports import _RunReadFailure

                raise _RunReadFailure
            return payload

        return LiveWorkflowRunReader(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            request=request,
        )

    def _reference(self, run_id="42"):
        return WorkflowRunReference(
            deployment_id=DEPLOYMENT_ID, repository_id=REPOSITORY_ID, run_id=run_id
        )

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self._reader(payload={}), WorkflowRunReader)

    def test_completed_run_is_parsed(self) -> None:
        reader = self._reader(
            payload={
                "path": APPLY_WORKFLOW,
                "head_sha": COMMIT,
                "conclusion": "success",
                "name": f"terraform-apply plan_hash={PLAN_HASH}",
            }
        )
        facts = reader.read_run(self._reference())
        self.assertIsInstance(facts, WorkflowRunFacts)
        self.assertIs(facts.conclusion, WorkflowConclusion.SUCCESS)
        self.assertEqual(facts.ref, COMMIT)
        self.assertEqual(facts.commit_sha, COMMIT)
        self.assertEqual(facts.plan_hash, PLAN_HASH)
        self.assertEqual(facts.workflow_path, APPLY_WORKFLOW)

    def test_http_failure_is_a_value_not_exception(self) -> None:
        facts = self._reader(fail=True).read_run(self._reference())
        self.assertIsInstance(facts, WorkflowRunFacts)
        self.assertIs(facts.conclusion, WorkflowConclusion.FAILURE)

    def test_incomplete_run_becomes_failure_value(self) -> None:
        reader = self._reader(
            payload={
                "path": APPLY_WORKFLOW,
                "head_sha": COMMIT,
                "conclusion": None,
                "name": f"terraform-apply plan_hash={PLAN_HASH}",
            }
        )
        self.assertIs(reader.read_run(self._reference()).conclusion, WorkflowConclusion.FAILURE)

    def test_missing_plan_hash_marker_becomes_failure_value(self) -> None:
        reader = self._reader(
            payload={
                "path": APPLY_WORKFLOW,
                "head_sha": COMMIT,
                "conclusion": "success",
                "name": "terraform-apply",
            }
        )
        self.assertIs(reader.read_run(self._reference()).conclusion, WorkflowConclusion.FAILURE)

    def test_read_rejects_out_of_scope(self) -> None:
        reader = self._reader(payload={})
        other = WorkflowRunReference(
            deployment_id=DEPLOYMENT_ID, repository_id="repo-other", run_id="42"
        )
        with self.assertRaises(LiveDeploymentPortError):
            reader.read_run(other)


class _FakeResourceTool(AwsResourceTool):
    def __init__(self) -> None:
        self.reads: list[str] = []
        self.lists: list[str] = []

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        self.reads.append(query.resource_id or "")
        return AwsResourceView(
            aws_account_id=query.aws_account_id,
            resource_type=query.resource_type,
            resource_id=query.resource_id or "",
            attributes={"encryption": {"enabled": True}},
        )

    def list_resources(self, query: AwsResourceQuery):
        self.lists.append(query.resource_type)
        return [
            AwsResourceView(
                aws_account_id=query.aws_account_id,
                resource_type=query.resource_type,
                resource_id=f"{query.resource_type}-1",
                attributes={"encryption": {"enabled": True}},
            )
        ]


class ActualRereadTest(unittest.TestCase):
    def _sync_target(self) -> RemediationSyncTarget:
        return RemediationSyncTarget(
            finding_id="find-1",
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT,
        )

    def _port(self, publish=None, tool=None) -> LiveActualRereadPort:
        return LiveActualRereadPort(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_tool=tool or _FakeResourceTool(),
            resource_types=("aws_s3_bucket", "aws_iam_role"),
            publish=publish,
        )

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self._port(), ActualRereadPort)

    def test_reread_reads_actual_via_resource_tool(self) -> None:
        # 재조회는 주입된 read-only Resource Tool을 반드시 호출해야 한다(publish 유무 무관).
        tool = _FakeResourceTool()
        port = self._port(tool=tool)
        result = port.reread_actual(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID, sync_target=self._sync_target()
        )
        self.assertIsNone(result)
        self.assertEqual(tool.lists, ["aws_s3_bucket", "aws_iam_role"])

    def test_reread_publishes_reread_views(self) -> None:
        tool = _FakeResourceTool()
        seen: list[tuple[str, RemediationSyncTarget, object]] = []
        port = self._port(publish=lambda dep, tgt, views: seen.append((dep, tgt, views)), tool=tool)
        port.reread_actual(
            customer_id=CUSTOMER_ID, deployment_id=DEPLOYMENT_ID, sync_target=self._sync_target()
        )
        self.assertEqual(tool.lists, ["aws_s3_bucket", "aws_iam_role"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], DEPLOYMENT_ID)
        # 두 resource type에서 각각 하나씩, 재조회된 view가 콜백으로 넘어간다.
        self.assertEqual(len(seen[0][2]), 2)

    def test_requires_at_least_one_resource_type(self) -> None:
        with self.assertRaises(ValueError):
            LiveActualRereadPort(
                customer_id=CUSTOMER_ID,
                aws_account_id=AWS_ACCOUNT_ID,
                resource_tool=_FakeResourceTool(),
                resource_types=(),
            )

    def test_rejects_out_of_scope(self) -> None:
        with self.assertRaises(LiveDeploymentPortError):
            self._port().reread_actual(
                customer_id="cust-other",
                deployment_id=DEPLOYMENT_ID,
                sync_target=self._sync_target(),
            )


if __name__ == "__main__":
    unittest.main()
