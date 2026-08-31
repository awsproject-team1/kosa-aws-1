"""Unit tests for the Assessment input collector combining D read-only tools."""

import unittest

from agent.context import (
    AssessmentInputBundle,
    AssessmentInputCollector,
    AwsResourceSelector,
    SnapshotReadRequest,
)
from agent.runtime import (
    AwsResourceNotFoundError,
    AwsResourceView,
    GitHubSnapshotNotFoundError,
    GitHubToolScopeError,
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
        # Both tools are scoped to CUSTOMER_ID. The collector reads IaC first,
        # so an out-of-scope customer is refused by the GitHub scope guard
        # before any AWS read happens -- the request never crosses tenants.
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
        # Contract-level rule: READ_RESOURCE without resource_id is invalid, so
        # building the query from the selector fails during collection.
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
