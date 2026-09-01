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

    def test_different_commit_yields_different_artifact_id(self) -> None:
        # [P2] immutable artifact identity: 같은 repository/finding이라도 commit이 다르면
        # 내용이 다르므로 artifact_id도 달라져야 한다. 같은 ID가 다른 내용을 가리키면 안 된다.
        base = generator().generate(finding_id="finding-001", snapshot=snapshot())
        other = generator().generate(finding_id="finding-001", snapshot=snapshot(commit="def456"))
        self.assertNotEqual(base.artifact.artifact_id, other.artifact.artifact_id)

    def test_artifact_id_includes_content_digest(self) -> None:
        # [P2] artifact_id는 내용을 유일하게 규정하는 content digest를 담아야 한다.
        # 이것이 "같은 ID = 같은 내용" 불변식의 근거다.
        patch = generator().generate(finding_id="finding-001", snapshot=snapshot())
        self.assertIn(patch.artifact.content_sha256, patch.artifact.artifact_id)

    def test_same_commit_different_plan_yields_different_artifact_id(self) -> None:
        # [P2 잔여] 같은 repository/finding/commit이라도 계획(changed_paths)이 다르면
        # 내용이 다르므로 artifact_id도 달라져야 한다. commit만 담던 이전 방식은 이 경우
        # 같은 ID가 다른 내용을 가리켰다. AI 재생성/계획 변경에서 실제로 발생하는 시나리오.
        plan_a = FixturePatchGenerator({"finding-001": ("main.tf",)})
        plan_b = FixturePatchGenerator({"finding-001": ("main.tf", "variables.tf")})
        a = plan_a.generate(finding_id="finding-001", snapshot=snapshot())
        b = plan_b.generate(finding_id="finding-001", snapshot=snapshot())
        # 같은 finding/commit이지만 내용이 다르므로,
        self.assertNotEqual(a.artifact.content_sha256, b.artifact.content_sha256)
        # artifact_id도 달라야 한다(불변식).
        self.assertNotEqual(a.artifact.artifact_id, b.artifact.artifact_id)

    def test_input_path_order_does_not_change_patch(self) -> None:
        # [P3] changed_paths 순서는 집합 의미만 가진다. 입력 순서만 다른 두 계획은
        # 같은 changed_paths(정렬)와 같은 digest로 정규화되어야 한다.
        forward = FixturePatchGenerator({"finding-001": ("a.tf", "b.tf")})
        reverse = FixturePatchGenerator({"finding-001": ("b.tf", "a.tf")})
        first = forward.generate(finding_id="finding-001", snapshot=snapshot())
        second = reverse.generate(finding_id="finding-001", snapshot=snapshot())
        # 반환되는 changed_paths가 정렬되어 동일하고,
        self.assertEqual(first.changed_paths, ("a.tf", "b.tf"))
        self.assertEqual(second.changed_paths, ("a.tf", "b.tf"))
        # digest도 동일하다(직렬화 결과와 hash의 일관성).
        self.assertEqual(first.artifact.content_sha256, second.artifact.content_sha256)

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
