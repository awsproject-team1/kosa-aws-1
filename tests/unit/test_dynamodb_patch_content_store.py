"""The DynamoDB patch content store is content-addressed and verifies what it reads back."""

import unittest

from apps.backend.remediation.patch_content import (
    PatchContentError,
    encode_patch_content,
    patch_content_digest,
)
from apps.backend.repositories.errors import StoredDataError
from apps.backend.repositories.remediation_patch_content import (
    ENTITY_TYPE,
    DynamoDbPatchContentStore,
)
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

CUSTOMER = "cust-001"
FINDING = "finding-abc"
COMMIT = "b" * 40
CHANGES = {"main.tf": 'resource "aws_s3_bucket" "x" {}\n'}
CONTENT = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
DIGEST = patch_content_digest(CONTENT)


def _patch(digest: str = DIGEST) -> RemediationPatch:
    return RemediationPatch(
        finding_id=FINDING,
        base_commit_sha=COMMIT,
        artifact=ArtifactReference(
            artifact_id=f"remediation-patch:repo-001:{FINDING}:{digest}",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256=digest,
            customer_id=CUSTOMER,
            repository_id="repo-001",
        ),
        changed_paths=("main.tf",),
    )


class ConditionalCheckFailed(Exception):
    def __init__(self) -> None:
        super().__init__("conditional check failed")
        self.response = {"Error": {"Code": "ConditionalCheckFailedException"}}


class FakeTable:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], dict[str, object]] = {}

    def put_item(self, *, Item, ConditionExpression=None):
        key = (Item["PK"], Item["SK"])
        if ConditionExpression and key in self.items:
            raise ConditionalCheckFailed()
        self.items[key] = dict(Item)

    def get_item(self, *, Key, ConsistentRead=False):
        item = self.items.get((Key["PK"], Key["SK"]))
        return {} if item is None else {"Item": dict(item)}


class PatchContentStoreTest(unittest.TestCase):
    def test_writes_under_the_digest_and_reads_back_verified_content(self) -> None:
        table = FakeTable()
        store = DynamoDbPatchContentStore(table)
        store.put(patch=_patch(), content=CONTENT)
        item = table.items[(f"CUSTOMER#{CUSTOMER}", f"REMEDIATION_PATCH#{DIGEST}")]
        self.assertEqual(item["entity_type"], ENTITY_TYPE)
        self.assertEqual(item["content_sha256"], DIGEST)
        self.assertEqual(item["byte_size"], len(CONTENT))
        self.assertEqual(store.get(patch=_patch()).changes, CHANGES)

    def test_an_identical_retry_is_absorbed(self) -> None:
        table = FakeTable()
        store = DynamoDbPatchContentStore(table)
        store.put(patch=_patch(), content=CONTENT)
        store.put(patch=_patch(), content=CONTENT)
        self.assertEqual(len(table.items), 1)

    def test_a_tampered_item_is_refused_on_read(self) -> None:
        table = FakeTable()
        store = DynamoDbPatchContentStore(table)
        store.put(patch=_patch(), content=CONTENT)
        item = table.items[(f"CUSTOMER#{CUSTOMER}", f"REMEDIATION_PATCH#{DIGEST}")]
        item["content"] = item["content"].replace("aws_s3_bucket", "aws_s3_bucket_public")
        with self.assertRaisesRegex(StoredDataError, "invalid"):
            store.get(patch=_patch())

    def test_missing_content_is_reported(self) -> None:
        with self.assertRaisesRegex(StoredDataError, "not stored"):
            DynamoDbPatchContentStore(FakeTable()).get(patch=_patch())

    def test_content_must_match_the_patch_digest_before_writing(self) -> None:
        other = encode_patch_content(
            finding_id=FINDING, base_commit_sha=COMMIT, changes={"main.tf": "other\n"}
        )
        with self.assertRaises(PatchContentError):
            DynamoDbPatchContentStore(FakeTable()).put(patch=_patch(), content=other)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
