"""Tenant-scope tests for A's Policy Source upload-session adapter."""

import unittest
from io import BytesIO

from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.policy.ingestion import normalize_upload
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository
from packages.contracts import PolicySourceUploadRequest


class Table:
    def __init__(self) -> None:
        self.item = None

    def put_item(self, **kwargs):
        self.item = kwargs["Item"]

    def get_item(self, **kwargs):
        key = kwargs["Key"]
        if self.item is None or (self.item["PK"], self.item["SK"]) != (key["PK"], key["SK"]):
            return {}
        return {"Item": self.item}

    def update_item(self, **kwargs):
        assert self.item is not None
        values = kwargs["ExpressionAttributeValues"]
        assert isinstance(values, dict)
        if self.item["status"] != values.get(":pending", values.get(":uploaded")):
            raise RuntimeError("conditional state failed")
        if ":status" not in values:
            self.item.update(
                {
                    "status": values[":uploaded"],
                    "s3_version_id": values[":version"],
                    "content_sha256": values[":digest"],
                }
            )
            return
        self.item.update(
            {
                "status": values[":status"],
                "detected_media_type": values[":detected"],
                "source_format": values[":format"],
                "parser_id": values[":parser_id"],
                "parser_version": values[":parser_version"],
                "normalized_artifact_id": values[":artifact"],
                "normalized_sha256": values[":digest"],
                "units": values[":units"],
                "warnings": values[":warnings"],
                "failure_code": values[":failure"],
            }
        )


class Presigner:
    def __init__(self) -> None:
        self.params = None

    def generate_presigned_url(self, ClientMethod, Params, ExpiresIn):
        self.params = (ClientMethod, Params, ExpiresIn)
        return "https://example.invalid/upload"


class S3(Presigner):
    def __init__(self) -> None:
        super().__init__()
        self.objects: dict[str, dict[str, object]] = {}

    def get_object(self, *, Bucket, Key):
        return self.objects[Key]

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = {"Body": BytesIO(Body), "ContentType": ContentType}


class PolicySourceUploadRepositoryTest(unittest.TestCase):
    def test_server_derives_a_customer_scoped_key(self) -> None:
        table, presigner = Table(), Presigner()
        session = DynamoDbPolicySourceUploadRepository(
            table=table, bucket="artifacts", presigner=presigner
        ).create_upload_session(
            customer_id="cust-a",
            request=PolicySourceUploadRequest(
                filename="policy.md", declared_media_type="text/markdown", byte_size=12
            ),
            source_id="source-1",
            source_version="v1",
        )
        self.assertEqual(session.source_id, "source-1")
        assert table.item is not None
        self.assertEqual(table.item["PK"], "CUSTOMER#cust-a")
        self.assertEqual(table.item["status"], "UPLOAD_PENDING")
        assert presigner.params is not None
        self.assertEqual(
            presigner.params[1]["Key"],
            "customers/cust-a/policy-sources/source-1/versions/v1/original",
        )

    def test_finalizes_server_selected_version_and_persists_normalized_artifact(self) -> None:
        table, s3 = Table(), S3()
        repository = DynamoDbPolicySourceUploadRepository(
            table=table, bucket="artifacts", presigner=s3
        )
        repository.create_upload_session(
            customer_id="cust-a",
            request=PolicySourceUploadRequest(
                filename="policy.md", declared_media_type="text/markdown", byte_size=38
            ),
            source_id="source-1",
            source_version="v1",
        )
        original_key = "customers/cust-a/policy-sources/source-1/versions/v1/original"
        payload = b"# Access\n\nPublic access is forbidden.\n"
        assert len(payload) == 38
        s3.objects[original_key] = {
            "Body": BytesIO(payload),
            "VersionId": "s3-v1",
            "ContentType": "text/markdown",
        }

        original, actual = repository.finalize_upload(
            customer_id="cust-a", source_id="source-1", source_version="v1", reader=s3
        )
        outcome = normalize_upload(original, actual)
        repository.record_normalization(
            customer_id="cust-a",
            document=outcome.document,
            normalized_payload=outcome.normalized_payload,
        )

        status = repository.get_document(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(status.status.value, "READY")
        self.assertEqual(status.content_sha256, original.content_sha256)
        self.assertIn("customers/cust-a/policy-sources/source-1/versions/v1/normalized", s3.objects)
        with self.assertRaises(LookupError):
            repository.get_document(customer_id="cust-b", source_id="source-1", source_version="v1")

    def test_policy_source_api_allows_only_admin_scope(self) -> None:
        table, s3 = Table(), S3()
        service = PolicySourceApiService(
            repository=DynamoDbPolicySourceUploadRepository(
                table=table, bucket="artifacts", presigner=s3
            ),
            source_id_factory=lambda: "source-1",
            source_version_factory=lambda: "v1",
        )
        user = Principal(
            subject="user", client_id="client", customer_id="cust-a", roles=frozenset({Role.USER})
        )
        with self.assertRaises(AuthorizationDenied):
            service.create_upload_session(
                user,
                PolicySourceUploadRequest(
                    filename="policy.md", declared_media_type="text/markdown", byte_size=1
                ),
            )
