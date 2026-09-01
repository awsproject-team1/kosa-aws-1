"""read-only D tool을 결합하는 Assessment 입력 collector에 대한 unit 테스트."""

import unittest

from agent.context import (
    AssessmentInputBundle,
    AssessmentInputCollector,
    AssessmentInputError,
    AwsResourceSelector,
    SnapshotReadRequest,
)
from agent.runtime import (
    AwsResourceNotFoundError,
    AwsResourceView,
    GitHubSnapshotNotFoundError,
    GitHubToolScopeError,
    IaCDocument,
    IaCSnapshotRequest,
    MockAwsResourceTool,
    MockGitHubTool,
)
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    AwsResourceOperation,
    IaCSnapshot,
)

CUSTOMER_ID = "cust-001"
REPOSITORY_ID = "repo-iac-001"
AWS_ACCOUNT_ID = "111122223333"
COMMIT_A = "a" * 40


def build_snapshot(*, commit_sha: str = COMMIT_A) -> IaCSnapshot:
    return IaCSnapshot(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        commit_sha=commit_sha,
        artifact=ArtifactReference(
            artifact_id=f"artifact-{commit_sha[:8]}",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="0" * 64,
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
        ),
    )


def build_view(*, resource_type: str, resource_id: str) -> AwsResourceView:
    return AwsResourceView(
        aws_account_id=AWS_ACCOUNT_ID,
        resource_type=resource_type,
        resource_id=resource_id,
        attributes={"encrypted": True},
    )


def build_github_tool() -> MockGitHubTool:
    return MockGitHubTool(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        snapshots=[build_snapshot()],
    )


def build_aws_tool() -> MockAwsResourceTool:
    return MockAwsResourceTool(
        customer_id=CUSTOMER_ID,
        aws_account_id=AWS_ACCOUNT_ID,
        resources=[
            build_view(resource_type="AWS::S3::Bucket", resource_id="bucket-a"),
            build_view(resource_type="AWS::S3::Bucket", resource_id="bucket-b"),
            build_view(resource_type="AWS::RDS::DBInstance", resource_id="db-a"),
        ],
    )


def build_collector() -> AssessmentInputCollector:
    return AssessmentInputCollector(
        github_tool=build_github_tool(),
        aws_tool=build_aws_tool(),
    )


def read_request(
    *,
    customer_id: str = CUSTOMER_ID,
    commit_sha: str = COMMIT_A,
    selectors: tuple[AwsResourceSelector, ...] | None = None,
) -> SnapshotReadRequest:
    if selectors is None:
        selectors = (
            AwsResourceSelector(
                operation=AwsResourceOperation.READ_RESOURCE,
                resource_type="AWS::S3::Bucket",
                resource_id="bucket-a",
            ),
        )
    return SnapshotReadRequest(
        customer_id=customer_id,
        repository_id=REPOSITORY_ID,
        commit_sha=commit_sha,
        aws_account_id=AWS_ACCOUNT_ID,
        aws_selectors=selectors,
    )


class AssessmentInputCollectorTest(unittest.TestCase):
    def test_collect_returns_iac_snapshot_and_aws_views(self) -> None:
        bundle = build_collector().collect(read_request())

        self.assertIsInstance(bundle, AssessmentInputBundle)
        self.assertEqual(bundle.customer_id, CUSTOMER_ID)
        self.assertEqual(bundle.iac_snapshot.commit_sha, COMMIT_A)
        self.assertEqual(len(bundle.aws_resources), 1)
        self.assertEqual(bundle.aws_resources[0].resource_id, "bucket-a")

    def test_collect_expands_list_selector_into_multiple_views(self) -> None:
        request = read_request(
            selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.LIST_RESOURCES,
                    resource_type="AWS::S3::Bucket",
                ),
            )
        )

        bundle = build_collector().collect(request)

        resource_ids = {view.resource_id for view in bundle.aws_resources}
        self.assertEqual(resource_ids, {"bucket-a", "bucket-b"})

    def test_collect_combines_multiple_selectors_in_order(self) -> None:
        request = read_request(
            selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.READ_RESOURCE,
                    resource_type="AWS::RDS::DBInstance",
                    resource_id="db-a",
                ),
                AwsResourceSelector(
                    operation=AwsResourceOperation.LIST_RESOURCES,
                    resource_type="AWS::S3::Bucket",
                ),
            )
        )

        bundle = build_collector().collect(request)

        self.assertEqual(bundle.aws_resources[0].resource_id, "db-a")
        self.assertEqual(len(bundle.aws_resources), 3)

    def test_collect_propagates_iac_not_found(self) -> None:
        with self.assertRaises(GitHubSnapshotNotFoundError):
            build_collector().collect(read_request(commit_sha="f" * 40))

    def test_collect_propagates_aws_not_found(self) -> None:
        request = read_request(
            selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.READ_RESOURCE,
                    resource_type="AWS::S3::Bucket",
                    resource_id="missing",
                ),
            )
        )

        with self.assertRaises(AwsResourceNotFoundError):
            build_collector().collect(request)

    def test_collect_rejects_other_customer_at_first_scoped_read(self) -> None:
        # 두 tool 모두 CUSTOMER_ID로 scope가 제한된다. collector는 IaC를 먼저 read하므로,
        # scope 밖 customer는 AWS read가 일어나기 전에 GitHub scope 가드에서 거부된다.
        # 요청이 tenant 경계를 넘지 않는다.
        with self.assertRaises(GitHubToolScopeError):
            build_collector().collect(read_request(customer_id="cust-999"))

    def test_bundle_is_immutable(self) -> None:
        bundle = build_collector().collect(read_request())

        with self.assertRaises(AttributeError):
            bundle.customer_id = "mutated"  # type: ignore[misc]

    def test_bundle_round_trips_to_dict(self) -> None:
        bundle = build_collector().collect(read_request())

        as_dict = bundle.to_dict()
        self.assertEqual(as_dict["customer_id"], CUSTOMER_ID)
        self.assertEqual(as_dict["iac_snapshot"]["commit_sha"], COMMIT_A)
        self.assertEqual(len(as_dict["aws_resources"]), 1)

    def test_collect_rejects_non_request_object(self) -> None:
        with self.assertRaises(TypeError):
            build_collector().collect(object())

    def test_request_rejects_empty_selectors(self) -> None:
        with self.assertRaises(ValueError):
            read_request(selectors=())

    def test_read_resource_selector_requires_resource_id(self) -> None:
        # Contract 수준 규칙: resource_id 없는 READ_RESOURCE는 유효하지 않으므로,
        # selector로 query를 만드는 시점(수집 중)에 실패한다.
        request = read_request(
            selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.READ_RESOURCE,
                    resource_type="AWS::S3::Bucket",
                ),
            )
        )

        with self.assertRaises(ValueError):
            build_collector().collect(request)


if __name__ == "__main__":
    unittest.main()


TERRAFORM_BODY = 'resource "aws_s3_bucket_public_access_block" "a" { block_public_acls = true }'


def build_document(*, commit_sha: str = COMMIT_A) -> IaCDocument:
    return IaCDocument(
        customer_id=CUSTOMER_ID,
        repository_id=REPOSITORY_ID,
        commit_sha=commit_sha,
        files=(("main.tf", TERRAFORM_BODY),),
    )


class SnapshotOnlyGitHubTool:
    """A tool that can read the snapshot reference but not the Terraform body."""

    def read_iac_snapshot(self, request: object) -> IaCSnapshot:
        return build_snapshot()


class IaCDocumentCollectionTest(unittest.TestCase):
    def collector(self, *, documents: tuple[IaCDocument, ...] = ()) -> AssessmentInputCollector:
        return AssessmentInputCollector(
            github_tool=MockGitHubTool(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                snapshots=[build_snapshot()],
                documents=documents,
            ),
            aws_tool=build_aws_tool(),
        )

    def test_document_is_absent_unless_the_request_asks_for_it(self) -> None:
        bundle = self.collector(documents=(build_document(),)).collect(read_request())

        self.assertIsNone(bundle.iac_document)

    def test_requested_document_joins_the_bundle_for_the_same_commit(self) -> None:
        request = SnapshotReadRequest(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT_A,
            aws_account_id=AWS_ACCOUNT_ID,
            aws_selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.READ_RESOURCE,
                    resource_type="AWS::S3::Bucket",
                    resource_id="bucket-a",
                ),
            ),
            include_iac_document=True,
        )

        bundle = self.collector(documents=(build_document(),)).collect(request)

        assert bundle.iac_document is not None
        self.assertEqual(bundle.iac_document.commit_sha, bundle.iac_snapshot.commit_sha)
        self.assertEqual(bundle.iac_document.evidence_references, ("terraform:main.tf",))
        self.assertEqual(bundle.to_dict()["iac_document"], bundle.iac_document.to_dict())

    def test_missing_document_for_the_pinned_commit_fails_closed(self) -> None:
        request = read_request()
        request = SnapshotReadRequest(
            customer_id=request.customer_id,
            repository_id=request.repository_id,
            commit_sha=request.commit_sha,
            aws_account_id=request.aws_account_id,
            aws_selectors=request.aws_selectors,
            include_iac_document=True,
        )

        with self.assertRaises(GitHubSnapshotNotFoundError):
            self.collector().collect(request)

    def test_tool_without_body_read_support_fails_instead_of_dropping_the_perspective(
        self,
    ) -> None:
        collector = AssessmentInputCollector(
            github_tool=SnapshotOnlyGitHubTool(), aws_tool=build_aws_tool()
        )
        request = SnapshotReadRequest(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            commit_sha=COMMIT_A,
            aws_account_id=AWS_ACCOUNT_ID,
            aws_selectors=(
                AwsResourceSelector(
                    operation=AwsResourceOperation.READ_RESOURCE,
                    resource_type="AWS::S3::Bucket",
                    resource_id="bucket-a",
                ),
            ),
            include_iac_document=True,
        )

        with self.assertRaisesRegex(AssessmentInputError, "Terraform body"):
            collector.collect(request)

    def test_bundle_rejects_a_document_from_another_commit(self) -> None:
        with self.assertRaisesRegex(AssessmentInputError, "snapshot commit"):
            AssessmentInputBundle(
                customer_id=CUSTOMER_ID,
                iac_snapshot=build_snapshot(),
                aws_resources=(),
                iac_document=build_document(commit_sha="b" * 40),
            )

    def test_document_rejects_duplicate_paths_and_empty_file_sets(self) -> None:
        with self.assertRaisesRegex(ValueError, "duplicate"):
            IaCDocument(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                commit_sha=COMMIT_A,
                files=(("main.tf", "a"), ("main.tf", "b")),
            )
        with self.assertRaisesRegex(ValueError, "non-empty tuple"):
            IaCDocument(
                customer_id=CUSTOMER_ID,
                repository_id=REPOSITORY_ID,
                commit_sha=COMMIT_A,
                files=(),
            )

    def test_mock_tool_rejects_documents_outside_its_scope(self) -> None:
        with self.assertRaisesRegex(ValueError, "document scope"):
            MockGitHubTool(
                customer_id=CUSTOMER_ID,
                repository_id="repo-other",
                snapshots=[],
                documents=(build_document(),),
            )

    def test_mock_tool_refuses_out_of_scope_document_reads(self) -> None:
        tool = MockGitHubTool(
            customer_id=CUSTOMER_ID,
            repository_id=REPOSITORY_ID,
            snapshots=[build_snapshot()],
            documents=(build_document(),),
        )

        with self.assertRaises(GitHubToolScopeError):
            tool.read_iac_document(
                IaCSnapshotRequest(
                    customer_id="cust-002", repository_id=REPOSITORY_ID, commit_sha=COMMIT_A
                )
            )
