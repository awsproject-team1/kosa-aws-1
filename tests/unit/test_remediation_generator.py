"""FixturePatchGenerator는 승인된 snapshot에 결정적으로 바인딩된 patch를 만든다."""

import unittest

from apps.backend.remediation import (
    FixturePatchGenerator,
    RemediationService,
)
from packages.contracts import ArtifactReference, ArtifactType, IaCSnapshot, RemediationPatch


def snapshot(
    *, customer: str = "cust-001", repository: str = "repo-001", commit: str = "abc123"
) -> IaCSnapshot:
    return IaCSnapshot(
        customer_id=customer,
        repository_id=repository,
        commit_sha=commit,
        artifact=ArtifactReference(
            artifact_id="art-snapshot",
            artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
            content_sha256="snapshot-digest",
            customer_id=customer,
            repository_id=repository,
        ),
    )


def generator() -> FixturePatchGenerator:
    return FixturePatchGenerator(
        {
            "finding-001": ("main.tf",),
            "finding-002": ("modules/s3/main.tf", "variables.tf"),
        }
    )


class FixturePatchGeneratorTest(unittest.TestCase):
    def test_binds_patch_to_requested_snapshot_and_finding(self) -> None:
        patch = generator().generate(finding_id="finding-001", snapshot=snapshot())
        self.assertIsInstance(patch, RemediationPatch)
        self.assertEqual(patch.finding_id, "finding-001")
        self.assertEqual(patch.base_commit_sha, "abc123")
        self.assertEqual(patch.artifact.artifact_type, ArtifactType.REMEDIATION_PATCH)
        self.assertEqual(patch.artifact.customer_id, "cust-001")
        self.assertEqual(patch.artifact.repository_id, "repo-001")
        self.assertEqual(patch.changed_paths, ("main.tf",))

    def test_output_passes_remediation_service_validation(self) -> None:
        # generator 출력이 검증 계층(RemediationService)을 그대로 통과해야 한다.
        service = RemediationService(generator())
        patch = service.generate(finding_id="finding-002", snapshot=snapshot())
        self.assertEqual(patch.finding_id, "finding-002")
        self.assertEqual(patch.changed_paths, ("modules/s3/main.tf", "variables.tf"))

    def test_generation_is_deterministic(self) -> None:
        # 같은 입력 → 같은 patch(특히 결정적 digest).
        first = generator().generate(finding_id="finding-001", snapshot=snapshot())
        second = generator().generate(finding_id="finding-001", snapshot=snapshot())
        self.assertEqual(first.artifact.content_sha256, second.artifact.content_sha256)
        self.assertEqual(first.artifact.artifact_id, second.artifact.artifact_id)

    def test_different_commit_yields_different_digest(self) -> None:
        # snapshot commit이 다르면 patch 내용 digest도 달라져야 한다(재실행 안전성).
        base = generator().generate(finding_id="finding-001", snapshot=snapshot())
        other = generator().generate(finding_id="finding-001", snapshot=snapshot(commit="def456"))
        self.assertNotEqual(base.artifact.content_sha256, other.artifact.content_sha256)

    def test_rejects_unknown_finding(self) -> None:
        with self.assertRaises(ValueError):
            generator().generate(finding_id="finding-999", snapshot=snapshot())

    def test_rejects_empty_finding_id(self) -> None:
        with self.assertRaises(ValueError):
            generator().generate(finding_id="  ", snapshot=snapshot())

    def test_rejects_non_snapshot(self) -> None:
        with self.assertRaises(TypeError):
            generator().generate(finding_id="finding-001", snapshot=object())

    def test_rejects_empty_plans(self) -> None:
        with self.assertRaises(ValueError):
            FixturePatchGenerator({})

    def test_rejects_plan_with_empty_paths(self) -> None:
        with self.assertRaises(ValueError):
            FixturePatchGenerator({"finding-001": ()})

    def test_rejects_absolute_changed_path_via_contract(self) -> None:
        # 절대경로는 RemediationPatch Contract가 생성 시점에 거부한다(경계 재사용 확인).
        gen = FixturePatchGenerator({"finding-001": ("/etc/passwd",)})
        with self.assertRaises(ValueError):
            gen.generate(finding_id="finding-001", snapshot=snapshot())

    def test_rejects_parent_traversal_path_via_contract(self) -> None:
        gen = FixturePatchGenerator({"finding-001": ("../secrets.tf",)})
        with self.assertRaises(ValueError):
            gen.generate(finding_id="finding-001", snapshot=snapshot())


if __name__ == "__main__":
    unittest.main()
