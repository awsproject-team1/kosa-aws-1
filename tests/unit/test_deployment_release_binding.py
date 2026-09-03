"""D producer의 M4 release 결합 digest 테스트 (ADR-0022 §4).

고정하는 불변식:
- 세 digest는 결정적이다(같은 입력 → 같은 digest).
- artifact set digest는 순서·중복에 무관하다.
- 형식 위반(짧은 commit, 비-SHA256 artifact, 빈 집합)은 fail-closed.
- **D가 만든 digest는 C parser의 `_digest`([0-9a-f]{64}) 검증을 통과한다**(경계 정합).
"""

import re
import unittest

from apps.backend.deployment.release_binding import (
    DeploymentReleaseBinding,
    ReleaseBindingError,
    derive_artifact_set_digest,
    derive_deployment_id_digest,
    derive_release_binding,
    derive_repository_commit_digest,
)

_SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = "a" * 40
ARTIFACTS = ("b" * 64, "c" * 64)


class ReleaseBindingTest(unittest.TestCase):
    def test_commit_digest_is_deterministic_and_sha256(self) -> None:
        first = derive_repository_commit_digest(COMMIT)
        self.assertIsNotNone(_SHA256.fullmatch(first))
        self.assertEqual(first, derive_repository_commit_digest(COMMIT))

    def test_commit_digest_rejects_a_short_or_uppercase_sha(self) -> None:
        with self.assertRaises(ReleaseBindingError):
            derive_repository_commit_digest("abc")
        with self.assertRaises(ReleaseBindingError):
            derive_repository_commit_digest("A" * 40)

    def test_deployment_id_digest_is_sha256(self) -> None:
        digest = derive_deployment_id_digest("dep-abc123")
        self.assertIsNotNone(_SHA256.fullmatch(digest))

    def test_deployment_id_digest_rejects_empty(self) -> None:
        with self.assertRaises(ReleaseBindingError):
            derive_deployment_id_digest("  ")

    def test_artifact_set_digest_is_order_and_duplicate_invariant(self) -> None:
        ordered = derive_artifact_set_digest(("b" * 64, "c" * 64))
        reversed_with_dup = derive_artifact_set_digest(("c" * 64, "b" * 64, "b" * 64))
        self.assertEqual(ordered, reversed_with_dup)
        self.assertIsNotNone(_SHA256.fullmatch(ordered))

    def test_artifact_set_digest_rejects_non_sha256_or_empty(self) -> None:
        with self.assertRaises(ReleaseBindingError):
            derive_artifact_set_digest(("not-a-sha",))
        with self.assertRaises(ReleaseBindingError):
            derive_artifact_set_digest(())
        with self.assertRaises(ReleaseBindingError):
            derive_artifact_set_digest("b" * 64)  # 문자열은 시퀀스로 받지 않는다.

    def test_derive_release_binding_bundles_three_digests(self) -> None:
        binding = derive_release_binding(
            commit_sha=COMMIT, deployment_id="dep-abc123", artifact_sha256s=ARTIFACTS
        )
        self.assertIsInstance(binding, DeploymentReleaseBinding)
        for value in binding.to_dict().values():
            self.assertIsNotNone(_SHA256.fullmatch(value))


class BoundaryWithConsumerTest(unittest.TestCase):
    """D producer 출력이 C parser의 관문(_digest)을 실제로 통과하는지 확인한다."""

    def test_digests_pass_the_consumer_digest_gate(self) -> None:
        from apps.backend.assessment.release_quality import (
            GoldenReleaseQualityError,
            _digest,
        )

        binding = derive_release_binding(
            commit_sha=COMMIT, deployment_id="dep-abc123", artifact_sha256s=ARTIFACTS
        )
        # C의 _digest는 통과 시 값을 그대로 돌려주고, 형식이 어긋나면 예외를 던진다.
        for name, value in binding.to_dict().items():
            try:
                self.assertEqual(_digest(value, name), value)
            except GoldenReleaseQualityError as error:  # pragma: no cover - 정합이면 안 남
                self.fail(f"consumer rejected D digest {name}: {error}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
