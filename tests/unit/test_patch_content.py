"""Patch bytes are canonical, size-bounded, and verified against the patch digest."""

import hashlib
import unittest

from apps.backend.remediation.patch_content import (
    MAX_PATCH_CONTENT_BYTES,
    InMemoryPatchContentStore,
    PatchContentError,
    decode_patch_content,
    encode_patch_content,
    patch_content_digest,
)
from packages.contracts import ArtifactReference, ArtifactType, RemediationPatch

FINDING = "finding-abc"
COMMIT = "b" * 40
CHANGES = {"main.tf": 'resource "aws_s3_bucket" "x" {}\n', "modules/s3/acl.tf": "# acl\n"}


def _patch(content: bytes, *, paths: tuple[str, ...] = ("main.tf", "modules/s3/acl.tf")):
    digest = patch_content_digest(content)
    return RemediationPatch(
        finding_id=FINDING,
        base_commit_sha=COMMIT,
        artifact=ArtifactReference(
            artifact_id=f"remediation-patch:repo:{FINDING}:{digest}",
            artifact_type=ArtifactType.REMEDIATION_PATCH,
            content_sha256=digest,
            customer_id="cust-001",
            repository_id="repo-001",
        ),
        changed_paths=paths,
    )


class EncodingTest(unittest.TestCase):
    def test_encoding_is_canonical_regardless_of_input_order(self) -> None:
        forward = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        reversed_changes = dict(reversed(list(CHANGES.items())))
        backward = encode_patch_content(
            finding_id=FINDING, base_commit_sha=COMMIT, changes=reversed_changes
        )
        self.assertEqual(forward, backward)
        self.assertEqual(patch_content_digest(forward), hashlib.sha256(forward).hexdigest())

    def test_round_trip(self) -> None:
        content = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        decoded = decode_patch_content(content)
        self.assertEqual(decoded.changes, CHANGES)
        self.assertEqual(decoded.changed_paths, ("main.tf", "modules/s3/acl.tf"))

    def test_non_canonical_bytes_are_refused(self) -> None:
        # 같은 의미이지만 key 순서가 다르다 — digest가 재현되지 않으므로 저장 경로 밖의 값이다.
        content = (
            b'{"finding_id":"finding-abc","base_commit_sha":"'
            + COMMIT.encode()
            + b'","changes":{"main.tf":"x"}}'
        )
        with self.assertRaisesRegex(PatchContentError, "canonical"):
            decode_patch_content(content)

    def test_paths_outside_the_repository_are_refused(self) -> None:
        with self.assertRaises(PatchContentError):
            encode_patch_content(
                finding_id=FINDING, base_commit_sha=COMMIT, changes={"../x.tf": "y"}
            )

    def test_size_limit_is_enforced(self) -> None:
        huge = {"main.tf": "x" * (MAX_PATCH_CONTENT_BYTES + 1)}
        with self.assertRaisesRegex(PatchContentError, "size limit"):
            encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=huge)


class InMemoryStoreTest(unittest.TestCase):
    def test_put_then_get_verifies_digest_and_identity(self) -> None:
        content = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        patch = _patch(content)
        store = InMemoryPatchContentStore()
        store.put(patch=patch, content=content)
        self.assertEqual(store.get(patch=patch).changes, CHANGES)

    def test_bytes_that_do_not_match_the_patch_digest_are_refused(self) -> None:
        content = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        other = encode_patch_content(
            finding_id=FINDING, base_commit_sha=COMMIT, changes={"main.tf": "different\n"}
        )
        with self.assertRaisesRegex(PatchContentError, "digest"):
            InMemoryPatchContentStore().put(patch=_patch(content), content=other)

    def test_content_that_changes_other_paths_than_the_patch_declares_is_refused(self) -> None:
        content = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        patch = _patch(content, paths=("main.tf",))
        store = InMemoryPatchContentStore()
        store.put(patch=patch, content=content)
        with self.assertRaisesRegex(PatchContentError, "different paths"):
            store.get(patch=patch)

    def test_missing_content_is_an_error_not_an_empty_change(self) -> None:
        content = encode_patch_content(finding_id=FINDING, base_commit_sha=COMMIT, changes=CHANGES)
        with self.assertRaisesRegex(PatchContentError, "not stored"):
            InMemoryPatchContentStore().get(patch=_patch(content))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
