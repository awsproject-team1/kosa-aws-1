"""M3 D live 실행 port 어댑터 Unit 테스트 (ADR-0019).

실제 GitHub/AWS 호출은 주입한 fake로 대체한다. 고정하는 것:
- 세 어댑터가 각 port Protocol을 만족한다.
- ApplyDispatchPort는 workflow_dispatch만 호출하고 input이 deployment_id/commit_sha/plan_hash다.
- 재조회 실패(404·형식 오류)는 예외가 아니라 실패 결론 값이다(EventBridge 불신뢰, section 7).
- 완료된 run은 path/head_sha/conclusion/run name의 plan_hash로 VerifiedRunOutcome이 된다.
- ActualRereadPort는 read-only Resource Tool을 재사용하고 없는 리소스는 건너뛴다.
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
from agent.runtime.aws_resource_tool import AwsResourceNotFoundError, AwsResourceTool
from packages.contracts import (
    ApplyRunReference,
    AwsResourceQuery,
    DeploymentApproval,
    VerifiedRunOutcome,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
REPO_FULL = "acme/iac"
AWS_ACCOUNT_ID = "111122223333"
DEPLOYMENT_ID = "dep-abc123"
PLAN_HASH = "f" * 64
COMMIT = "a" * 40
LINEAGE = "11111111-2222-3333-4444-555555555555"
RESOURCE_ID = "logs-bucket"


def build_approval() -> DeploymentApproval:
    return DeploymentApproval(
        deployment_id=DEPLOYMENT_ID,
        approved_by="admin-1",
        commit_sha=COMMIT,
        plan_hash=PLAN_HASH,
    )


class ApplyDispatchTest(unittest.TestCase):
    def _capture(self):
        calls: list[tuple[str, dict[str, str], bytes]] = []

        def dispatch(url, headers, body):
            calls.append((url, dict(headers), body))

        return calls, dispatch

    def test_protocol_conformance(self) -> None:
        calls, dispatch = self._capture()
        port = LiveApplyDispatchPort(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            dispatch=dispatch,
        )
        self.assertIsInstance(port, ApplyDispatchPort)

    def test_dispatch_posts_workflow_dispatch_with_bound_inputs(self) -> None:
        calls, dispatch = self._capture()
        port = LiveApplyDispatchPort(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            dispatch=dispatch,
        )
        reference = port.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        self.assertIsInstance(reference, ApplyRunReference)
        self.assertEqual(reference.deployment_id, DEPLOYMENT_ID)
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

    def test_dispatch_is_deterministic_per_approval(self) -> None:
        calls, dispatch = self._capture()
        port = LiveApplyDispatchPort(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            dispatch=dispatch,
        )
        first = port.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        second = port.dispatch_apply(
            approval=build_approval(),
            state_lineage=LINEAGE,
            state_serial=7,
            repository_id=REPOSITORY_ID,
        )
        self.assertEqual(first.run_id, second.run_id)

    def test_dispatch_rejects_out_of_scope_repository(self) -> None:
        _, dispatch = self._capture()
        port = LiveApplyDispatchPort(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            repository_full_name=REPO_FULL,
            token_provider=lambda: "tok",
            dispatch=dispatch,
        )
        with self.assertRaises(LiveDeploymentPortError):
            port.dispatch_apply(
                approval=build_approval(),
                state_lineage=LINEAGE,
                state_serial=7,
                repository_id="repo-other",
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

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self._reader(payload={}), WorkflowRunReader)

    def test_completed_run_is_parsed(self) -> None:
        reader = self._reader(
            payload={
                "path": ".github/workflows/terraform-apply.yml",
                "head_sha": COMMIT,
                "conclusion": "success",
                "name": f"terraform-apply plan_hash={PLAN_HASH}",
            }
        )
        outcome = reader.read_run(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="42")
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.ref, COMMIT)
        self.assertEqual(outcome.plan_hash, PLAN_HASH)
        self.assertEqual(outcome.workflow_path, ".github/workflows/terraform-apply.yml")

    def test_http_failure_is_a_value_not_exception(self) -> None:
        reader = self._reader(fail=True)
        outcome = reader.read_run(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="42")
        self.assertIsInstance(outcome, VerifiedRunOutcome)
        self.assertFalse(outcome.succeeded)
        self.assertEqual(outcome.conclusion, "not_found")

    def test_incomplete_run_becomes_failure_value(self) -> None:
        # conclusion=null(진행 중)이면 성공/실패가 아니므로 not_found 값으로 떨어진다.
        reader = self._reader(
            payload={
                "path": ".github/workflows/terraform-apply.yml",
                "head_sha": COMMIT,
                "conclusion": None,
                "name": f"terraform-apply plan_hash={PLAN_HASH}",
            }
        )
        outcome = reader.read_run(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="42")
        self.assertEqual(outcome.conclusion, "not_found")

    def test_missing_plan_hash_marker_becomes_failure_value(self) -> None:
        reader = self._reader(
            payload={
                "path": ".github/workflows/terraform-apply.yml",
                "head_sha": COMMIT,
                "conclusion": "success",
                "name": "terraform-apply",  # plan_hash 마커 없음
            }
        )
        outcome = reader.read_run(customer_id=CUSTOMER_ID, repository_id=REPOSITORY_ID, run_id="42")
        self.assertEqual(outcome.conclusion, "not_found")

    def test_read_rejects_out_of_scope(self) -> None:
        reader = self._reader(payload={})
        with self.assertRaises(LiveDeploymentPortError):
            reader.read_run(customer_id="cust-other", repository_id=REPOSITORY_ID, run_id="42")


class _FakeResourceTool(AwsResourceTool):
    def __init__(self, present: set[str]) -> None:
        self._present = present

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        if query.resource_id not in self._present:
            raise AwsResourceNotFoundError("not found")
        return AwsResourceView(
            aws_account_id=query.aws_account_id,
            resource_type=query.resource_type,
            resource_id=query.resource_id or "",
            attributes={"encryption": {"enabled": True}},
        )

    def list_resources(self, query: AwsResourceQuery):
        raise NotImplementedError


class ActualRereadTest(unittest.TestCase):
    def _port(self, present: set[str]) -> LiveActualRereadPort:
        return LiveActualRereadPort(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_tool=_FakeResourceTool(present),
        )

    def test_protocol_conformance(self) -> None:
        self.assertIsInstance(self._port(set()), ActualRereadPort)

    def test_rereads_present_resources_and_skips_missing(self) -> None:
        port = self._port({RESOURCE_ID})
        result = port.reread_actual(
            customer_id=CUSTOMER_ID,
            aws_account_id=AWS_ACCOUNT_ID,
            resource_ids=(RESOURCE_ID, "missing-bucket"),
        )
        self.assertEqual([s.resource_id for s in result], [RESOURCE_ID])
        # attributes는 문자열 매핑으로 직렬화된다.
        self.assertIsInstance(result[0].attributes["encryption"], str)

    def test_rejects_out_of_scope(self) -> None:
        port = self._port({RESOURCE_ID})
        with self.assertRaises(LiveDeploymentPortError):
            port.reread_actual(
                customer_id=CUSTOMER_ID,
                aws_account_id="999988887777",
                resource_ids=(RESOURCE_ID,),
            )


if __name__ == "__main__":
    unittest.main()
