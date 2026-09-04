"""Tenant-scope tests for A's Policy Source upload-session adapter."""

import unittest
from decimal import Decimal
from io import BytesIO

from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs.errors import sanitize_public_failure
from apps.backend.policy.ingestion import normalize_upload
from apps.backend.repositories.policy_ingestion import DynamoDbPolicySourceUploadRepository
from packages.common.errors import PolicySourceDeleteForbidden, PolicySourceNotFound
from packages.contracts import PolicySourceUploadRequest

#: 목록 요약이 정책 원문을 실어 나르지 않는다는 것을 확인하기 위한 표식 문자열.
POLICY_TEXT = "정책 원문 한 줄"


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

    def test_finalizes_when_byte_size_is_decimal_as_dynamodb_returns(self) -> None:
        # The DynamoDB resource API deserializes Number attributes to Decimal, so the stored
        # byte_size is a Decimal at finalize time, not an int. finalize_upload must accept it;
        # otherwise every real upload fails metadata validation with a 500 (regression).

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
        # Emulate the resource-API round trip: the persisted Number comes back as Decimal.
        assert table.item is not None
        table.item["byte_size"] = Decimal(table.item["byte_size"])

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
        self.assertEqual(original.byte_size, 38)

    def test_document_from_item_reads_decimal_numbers(self) -> None:
        # The authoring worker and get_document restore a NormalizedPolicyDocument from a DynamoDB
        # item whose Number attributes (byte_size, unit text_length) are Decimal. document_from_item
        # must accept them; otherwise candidate extraction and status reads fail (regression).
        from decimal import Decimal

        from apps.backend.repositories.policy_ingestion import document_from_item

        item = {
            "source_id": "source-1",
            "source_version": "v1",
            "artifact_id": "art-1",
            "s3_version_id": "s3-v1",
            "content_sha256": "d" * 64,
            "filename": "policy.md",
            "declared_media_type": "text/markdown",
            "byte_size": Decimal(38),
            "status": "READY",
            "detected_media_type": "text/markdown",
            "source_format": "MARKDOWN",
            "parser_id": "markdown",
            "parser_version": "1",
            "normalized_artifact_id": "norm-1",
            "normalized_sha256": "e" * 64,
            "units": [
                {
                    "locator": "heading/access-control",
                    "kind": "SECTION",
                    "text_sha256": "f" * 64,
                    "text_length": Decimal(18),
                    "origin": "line/1",
                }
            ],
            "warnings": [],
            "failure_code": None,
        }
        document = document_from_item(item)
        self.assertEqual(document.byte_size, 38)
        self.assertEqual(document.units[0].text_length, 18)

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


class DeleteTable:
    """A table fake that enforces the delete's ConditionExpression.

    The condition is the whole tenant guard on this path, so a fake that ignored it would let the
    delete tests pass against a repository that had dropped it.
    """

    def __init__(self, items: dict[tuple[str, str], dict] | None = None) -> None:
        self.items = dict(items or {})
        self.deleted: list[tuple[str, str]] = []
        self.queries: list[dict] = []
        self.pages: list[dict] | None = None

    def put_item(self, **kwargs):
        raise AssertionError("not used")

    def update_item(self, **kwargs):
        raise AssertionError("not used")

    def get_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        return {"Item": item} if item is not None else {}

    def query(self, **kwargs):
        self.queries.append(kwargs)
        if self.pages is not None:
            index = 0 if "ExclusiveStartKey" not in kwargs else int(kwargs["ExclusiveStartKey"])
            return self.pages[index]
        values = kwargs["ExpressionAttributeValues"]
        return {
            # DynamoDB가 그렇듯 key 속성은 항목에 들어 있다. 접두사 경계 판정이 SK를 읽으므로
            # fake가 그것을 빼면 그 판정이 테스트되지 않는다.
            "Items": [
                {"PK": pk, "SK": sk, **item}
                for (pk, sk), item in self.items.items()
                if pk == values[":pk"] and sk.startswith(values[":sk"])
            ]
        }

    def delete_item(self, **kwargs):
        key = (kwargs["Key"]["PK"], kwargs["Key"]["SK"])
        item = self.items.get(key)
        values = kwargs.get("ExpressionAttributeValues")
        if values is not None:
            # 조건식이 붙은 삭제만 조건을 강제한다. 자식 item 삭제는 이미 호출자 파티션에 고정된
            # query가 돌려준 키를 지우므로 조건이 없다.
            if item is None or item.get("customer_id") != values[":customer"]:
                raise ConditionalCheckFailed()
        elif item is None:
            return
        del self.items[key]
        self.deleted.append(key)


class ConditionalCheckFailed(Exception):
    """The botocore shape a refused ConditionExpression arrives in."""

    def __init__(self) -> None:
        super().__init__("ConditionalCheckFailedException")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class DeletingS3(S3):
    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.deleted: list[str] = []
        self.fail = fail

    def delete_object(self, *, Bucket, Key):
        if self.fail:
            raise RuntimeError("s3 unavailable")
        self.deleted.append(Key)


def _ingestion_item(customer_id: str, source_id: str = "source-1", version: str = "v1") -> dict:
    return {
        "PK": f"CUSTOMER#{customer_id}",
        "SK": f"POLICY_INGESTION#{source_id}#VERSION#{version}",
        "entity_type": "POLICY_INGESTION",
        "customer_id": customer_id,
        "source_id": source_id,
        "source_version": version,
        "filename": "policy.md",
        "declared_media_type": "text/markdown",
        "byte_size": Decimal(38),
        "status": "NORMALIZED",
        "source_format": "MARKDOWN",
        "artifact_id": "policy-original-source-1-v1",
        "units": [{"locator": "line/1", "text": POLICY_TEXT, "kind": "PARAGRAPH"}],
    }


class PolicySourceListTest(unittest.TestCase):
    def test_the_query_is_pinned_to_the_callers_partition(self) -> None:
        table = DeleteTable(
            {
                ("CUSTOMER#cust-a", "POLICY_INGESTION#source-1#VERSION#v1"): _ingestion_item(
                    "cust-a"
                ),
                ("CUSTOMER#cust-b", "POLICY_INGESTION#source-9#VERSION#v1"): _ingestion_item(
                    "cust-b", "source-9"
                ),
            }
        )
        repository = DynamoDbPolicySourceUploadRepository(
            table=table, bucket="artifacts", presigner=DeletingS3()
        )
        sources = repository.list_sources(customer_id="cust-a")
        self.assertEqual([s["source_id"] for s in sources], ["source-1"])
        self.assertEqual(table.queries[0]["ExpressionAttributeValues"][":pk"], "CUSTOMER#cust-a")

    def test_the_summary_never_carries_policy_text(self) -> None:
        """A list view is a directory, not a reader: unit text must not leave the partition."""
        table = DeleteTable(
            {("CUSTOMER#cust-a", "POLICY_INGESTION#source-1#VERSION#v1"): _ingestion_item("cust-a")}
        )
        repository = DynamoDbPolicySourceUploadRepository(
            table=table, bucket="artifacts", presigner=DeletingS3()
        )
        (source,) = repository.list_sources(customer_id="cust-a")
        self.assertNotIn(POLICY_TEXT, str(source))
        self.assertEqual(source["unit_count"], 1)
        self.assertEqual(source["byte_size"], 38)
        self.assertIsInstance(source["byte_size"], int)

    def test_pagination_is_followed_to_the_last_page(self) -> None:
        table = DeleteTable()
        table.pages = [
            {"Items": [_ingestion_item("cust-a")], "LastEvaluatedKey": "1"},
            {"Items": [_ingestion_item("cust-a", "source-2")]},
        ]
        repository = DynamoDbPolicySourceUploadRepository(
            table=table, bucket="artifacts", presigner=DeletingS3()
        )
        sources = repository.list_sources(customer_id="cust-a")
        self.assertEqual([s["source_id"] for s in sources], ["source-1", "source-2"])


class PolicySourceDeleteTest(unittest.TestCase):
    def _repository(self, table, s3):
        return DynamoDbPolicySourceUploadRepository(table=table, bucket="artifacts", presigner=s3)

    @staticmethod
    def _one_source(owner: str = "cust-a") -> DeleteTable:
        return DeleteTable(
            {("CUSTOMER#cust-a", "POLICY_INGESTION#source-1#VERSION#v1"): _ingestion_item(owner)}
        )

    def test_the_record_goes_before_the_artifacts(self) -> None:
        """Ordering is the recoverability call: orphaned bytes beat a record pointing at none."""
        table, s3 = self._one_source(), DeletingS3()
        self._repository(table, s3).delete_source(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(
            table.deleted, [("CUSTOMER#cust-a", "POLICY_INGESTION#source-1#VERSION#v1")]
        )
        self.assertEqual(
            s3.deleted,
            [
                "customers/cust-a/policy-sources/source-1/versions/v1/original",
                "customers/cust-a/policy-sources/source-1/versions/v1/normalized",
            ],
        )

    def test_an_approved_source_is_refused_and_nothing_is_deleted(self) -> None:
        table = self._one_source()
        table.items[("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1#APPROVAL")] = {
            "entity_type": "POLICY_SOURCE_APPROVAL"
        }
        s3 = DeletingS3()
        with self.assertRaises(PolicySourceDeleteForbidden):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(table.deleted, [])
        self.assertEqual(s3.deleted, [])

    def test_a_missing_source_is_not_found_rather_than_a_server_fault(self) -> None:
        table, s3 = DeleteTable(), DeletingS3()
        with self.assertRaises(PolicySourceNotFound):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(s3.deleted, [])

    def test_another_customers_record_is_not_reachable(self) -> None:
        """Key prefix and the stored customer_id both guard it, so neither alone is the boundary."""
        table, s3 = self._one_source(owner="cust-b"), DeletingS3()
        with self.assertRaises(PolicySourceNotFound):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(s3.deleted, [])

    def test_every_item_of_the_version_goes_not_just_the_ingestion_record(self) -> None:
        """A version is a record plus its authoring items; deleting one of them is not a delete.

        예전에는 `POLICY_INGESTION#` 하나만 지웠고, 후보·추출 요청은 테이블에 남았다. 남은
        `#REQUEST`는 사라진 문서를 가리키는 추출 요청이라 worker가 계속 그것을 집어 든다.
        """
        table, s3 = self._one_source(), DeletingS3()
        for suffix in ("", "#REQUEST", "#AUTHORING", "#CANDIDATE#001", "#REJECTED#001"):
            table.items[("CUSTOMER#cust-a", f"POLICY_SOURCE#source-1#VERSION#v1{suffix}")] = {
                "customer_id": "cust-a"
            }
        self._repository(table, s3).delete_source(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(
            sorted(sort_key for _pk, sort_key in table.deleted),
            [
                "POLICY_INGESTION#source-1#VERSION#v1",
                "POLICY_SOURCE#source-1#VERSION#v1",
                "POLICY_SOURCE#source-1#VERSION#v1#AUTHORING",
                "POLICY_SOURCE#source-1#VERSION#v1#CANDIDATE#001",
                "POLICY_SOURCE#source-1#VERSION#v1#REJECTED#001",
                "POLICY_SOURCE#source-1#VERSION#v1#REQUEST",
            ],
        )
        self.assertEqual(table.items, {})

    def test_a_neighbouring_version_that_shares_the_key_prefix_survives(self) -> None:
        """`v1` is a prefix of `v10`: the boundary is the `#`, not the query."""
        table, s3 = self._one_source(), DeletingS3()
        table.items[("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1#REQUEST")] = {
            "customer_id": "cust-a"
        }
        keep = ("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v10#REQUEST")
        table.items[keep] = {"customer_id": "cust-a"}
        self._repository(table, s3).delete_source(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(list(table.items), [keep])

    def test_another_customers_items_are_never_in_the_delete_set(self) -> None:
        table, s3 = self._one_source(), DeletingS3()
        other = ("CUSTOMER#cust-b", "POLICY_SOURCE#source-1#VERSION#v1#REQUEST")
        table.items[other] = {"customer_id": "cust-b"}
        self._repository(table, s3).delete_source(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(list(table.items), [other])

    def test_the_children_go_only_after_the_record_is_observably_gone(self) -> None:
        table, s3 = self._one_source(), DeletingS3()
        table.items[("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1#REQUEST")] = {
            "customer_id": "cust-a"
        }
        self._repository(table, s3).delete_source(
            customer_id="cust-a", source_id="source-1", source_version="v1"
        )
        self.assertEqual(table.deleted[0][1], "POLICY_INGESTION#source-1#VERSION#v1")

    def test_an_approved_source_keeps_its_authoring_items_too(self) -> None:
        table = self._one_source()
        table.items[("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1#APPROVAL")] = {
            "entity_type": "POLICY_SOURCE_APPROVAL"
        }
        table.items[("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1#CANDIDATE#001")] = {
            "customer_id": "cust-a"
        }
        s3 = DeletingS3()
        with self.assertRaises(PolicySourceDeleteForbidden):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(table.deleted, [])

    def test_a_missing_record_does_not_reach_the_child_sweep(self) -> None:
        """404 경로에서 자식을 지우면 ingestion record 없이 존재하는 seed 판본을 부순다."""
        table, s3 = DeleteTable(), DeletingS3()
        seeded = ("CUSTOMER#cust-a", "POLICY_SOURCE#source-1#VERSION#v1")
        table.items[seeded] = {"customer_id": "cust-a"}
        with self.assertRaises(PolicySourceNotFound):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(list(table.items), [seeded])

    def test_an_artifact_failure_surfaces_after_the_record_is_gone(self) -> None:
        table, s3 = self._one_source(), DeletingS3(fail=True)
        with self.assertRaises(RuntimeError):
            self._repository(table, s3).delete_source(
                customer_id="cust-a", source_id="source-1", source_version="v1"
            )
        self.assertEqual(
            table.deleted, [("CUSTOMER#cust-a", "POLICY_INGESTION#source-1#VERSION#v1")]
        )


class PolicySourceDeletePublicFailureTest(unittest.TestCase):
    """The public mapping is the reason these exception types exist; pin both statuses."""

    def test_a_refused_delete_is_a_conflict_not_a_server_error(self) -> None:
        failure = sanitize_public_failure(PolicySourceDeleteForbidden("approved"))
        self.assertEqual((failure.status_code, failure.error.code), (409, "CONFLICT"))

    def test_a_missing_source_is_a_not_found(self) -> None:
        failure = sanitize_public_failure(PolicySourceNotFound("missing"))
        self.assertEqual((failure.status_code, failure.error.code), (404, "NOT_FOUND"))
