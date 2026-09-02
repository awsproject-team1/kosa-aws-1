"""A/C policy lifecycle integration: upload through approval/profile stays tenant scoped."""

import unittest
from io import BytesIO

from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.auth import Principal, Role
from apps.backend.policy import InMemoryPolicyCatalog, PolicyContextResolver
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository
from packages.contracts import (
    AssessmentPhase,
    PolicyRule,
    PolicyRuleReference,
    PolicySource,
    PolicySourceKind,
    PolicySourceUploadRequest,
    RuleCandidate,
    RuleSeverity,
    SourceReference,
)


class Table:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, **kwargs: object) -> None:
        item = kwargs["Item"]
        assert isinstance(item, dict)
        key = (item["PK"], item["SK"])
        if key in self.items:
            raise RuntimeError("conditional write failed")
        self.items[key] = dict(item)

    def get_item(self, **kwargs: object) -> dict[str, object]:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items.get((key["PK"], key["SK"]))
        return {} if item is None else {"Item": item}

    def update_item(self, **kwargs: object) -> None:
        key = kwargs["Key"]
        assert isinstance(key, dict)
        item = self.items[(key["PK"], key["SK"])]
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        expected = values.get(":pending", values.get(":uploaded"))
        if item["status"] != expected:
            raise RuntimeError("conditional state failed")
        if ":status" not in values:
            item.update(
                status=values[":uploaded"],
                s3_version_id=values[":version"],
                content_sha256=values[":digest"],
            )
            return
        item.update(
            status=values[":status"],
            detected_media_type=values[":detected"],
            source_format=values[":format"],
            parser_id=values[":parser_id"],
            parser_version=values[":parser_version"],
            normalized_artifact_id=values[":artifact"],
            normalized_sha256=values[":digest"],
            units=values[":units"],
            warnings=values[":warnings"],
            failure_code=values[":failure"],
        )


class S3:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        return "https://example.invalid/upload"

    def get_object(self, *, Bucket, Key):
        return self.objects[Key]

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = {"Body": BytesIO(Body), "ContentType": ContentType}


class ReviewRepository:
    def __init__(self, documents: dict[tuple[str, str, str], object]) -> None:
        self.documents = documents
        self.approvals: dict[tuple[str, str, str], object] = {}
        self.profiles: dict[tuple[str, str], object] = {}

    def load_review(self, *, customer_id, source_id, source_version):
        document = self.documents[(customer_id, source_id, source_version)]
        unit = document.units[0]
        rule = PolicyRule(
            rule_id="RULE-1",
            version="v1",
            title="Rule",
            severity=RuleSeverity.HIGH,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(
                SourceReference(
                    source_id=source_id,
                    source_version=source_version,
                    locator=unit.locator,
                    content_sha256=unit.text_sha256,
                ),
            ),
        )
        return document, (RuleCandidate(rule=rule),)

    def record_approval(self, *, customer_id, approval, candidates):
        self.approvals[(customer_id, approval.source_id, approval.source_version)] = (
            approval,
            candidates,
        )

    def load_publication(self, *, customer_id, source_id, source_version):
        approval, candidates = self.approvals[(customer_id, source_id, source_version)]
        return (
            candidates,
            (approval,),
            (
                PolicySource(
                    source_id=source_id,
                    kind=PolicySourceKind.INTERNAL_POLICY,
                    title="Customer policy",
                    version=source_version,
                    artifact_id=approval.artifact_id,
                    content_sha256=approval.content_sha256,
                ),
            ),
        )

    def record_profile(self, *, customer_id, profile, published_by, published_at):
        self.profiles[(customer_id, profile.policy_profile_id)] = profile


class PolicyIngestionLifecycleTest(unittest.TestCase):
    def test_customer_a_lifecycle_cannot_be_read_or_published_by_customer_b(self) -> None:
        table, s3 = Table(), S3()
        upload = PolicySourceApiService(
            repository=DynamoDbPolicySourceUploadRepository(
                table=table, bucket="artifacts", presigner=s3
            ),
            source_id_factory=lambda: "source-1",
            source_version_factory=lambda: "v1",
        )
        admin_a = Principal(
            subject="admin-a",
            client_id="client",
            customer_id="cust-a",
            roles=frozenset({Role.ADMIN}),
        )
        admin_b = Principal(
            subject="admin-b",
            client_id="client",
            customer_id="cust-b",
            roles=frozenset({Role.ADMIN}),
        )
        session = upload.create_upload_session(
            admin_a,
            PolicySourceUploadRequest(
                filename="policy.md", declared_media_type="text/markdown", byte_size=38
            ),
        )
        key = "customers/cust-a/policy-sources/source-1/versions/v1/original"
        s3.objects[key] = {
            "Body": BytesIO(b"# Access\n\nPublic access is forbidden.\n"),
            "VersionId": "s3-v1",
            "ContentType": "text/markdown",
        }
        document = upload.process_upload(
            admin_a, source_id=session.source_id, source_version=session.source_version, reader=s3
        )
        with self.assertRaises(LookupError):
            upload.get_status(
                admin_b, source_id=session.source_id, source_version=session.source_version
            )

        reviews = ReviewRepository({("cust-a", "source-1", "v1"): document})
        approvals = PolicyApprovalApiService(reviews)
        approvals.approve(
            admin_a,
            source_id="source-1",
            source_version="v1",
            approved_rules=(PolicyRuleReference(rule_id="RULE-1", version="v1"),),
        )
        profile = approvals.publish(
            admin_a,
            source_id="source-1",
            source_version="v1",
            policy_profile_id="profile-1",
            version="v1",
        )
        self.assertEqual(profile.policy_profile_id, "profile-1")
        _, candidates = reviews.approvals[("cust-a", "source-1", "v1")]
        catalog = InMemoryPolicyCatalog(
            profiles=(profile,),
            rules=tuple(candidate.rule for candidate in candidates),
        )
        context = PolicyContextResolver(catalog).resolve(
            policy_profile_id="profile-1",
            resource_type="AWS::S3::Bucket",
            phase=AssessmentPhase.INITIAL,
        )
        self.assertEqual(tuple(rule.rule_id for rule in context.rules), ("RULE-1",))
        with self.assertRaises(KeyError):
            approvals.publish(
                admin_b,
                source_id="source-1",
                source_version="v1",
                policy_profile_id="profile-1",
                version="v1",
            )
