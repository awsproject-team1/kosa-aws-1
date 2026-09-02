"""`terraform show -json` 투영과 `plan_hash` 산출의 Contract 테스트 (ADR-0019 §1).

ADR-0019가 참이라면 아래가 고정될 수 있어야 한다.
- `plan_hash`는 같은 plan에서 두 번 계산해도 같다 (불변식 2).
- 투영은 허용 목록이라, Terraform/Provider가 출력 필드를 늘려도 hash가 흔들리지 않는다 (§1).
- 파괴적 변경(`delete` 또는 비어 있지 않은 `replace_paths`)이 판정된다 (불변식 8).
"""

import unittest

from packages.contracts import (
    TerraformPlanProjectionError,
    canonical_plan_bytes,
    compute_plan_hash,
    has_destructive_changes,
    project_plan,
)


def _plan(*resource_changes: dict[str, object]) -> dict[str, object]:
    return {"resource_changes": list(resource_changes)}


def _change(actions: list[str], **extra: object) -> dict[str, object]:
    change: dict[str, object] = {"actions": actions}
    change.update(extra)
    return change


class TerraformPlanProjectionTest(unittest.TestCase):
    def test_plan_hash_is_stable_across_recomputation(self) -> None:
        plan = _plan(
            {
                "address": "aws_s3_bucket.logs",
                "type": "aws_s3_bucket",
                "name": "logs",
                "change": _change(["update"], before={"acl": "public"}, after={"acl": "private"}),
            }
        )

        self.assertEqual(compute_plan_hash(plan), compute_plan_hash(plan))

    def test_projection_ignores_fields_outside_the_allow_list(self) -> None:
        base = {
            "address": "aws_s3_bucket.logs",
            "type": "aws_s3_bucket",
            "name": "logs",
            "change": _change(["no-op"]),
        }
        with_noise = dict(base)
        # Terraform/Provider가 늘릴 수 있는 필드들을 추가해도 hash가 흔들리면 안 된다.
        with_noise["provider_version"] = "5.31.0"
        with_noise["change"] = {**base["change"], "importing": {"id": "logs"}}
        noisy_plan = _plan(with_noise)
        noisy_plan["format_version"] = "1.2"
        noisy_plan["terraform_version"] = "1.9.5"
        noisy_plan["timestamp"] = "2026-09-02T00:00:00Z"
        noisy_plan["prior_state"] = {"anything": True}

        self.assertEqual(compute_plan_hash(_plan(base)), compute_plan_hash(noisy_plan))

    def test_projection_sorts_by_address(self) -> None:
        forward = _plan(
            {"address": "b.two", "change": _change(["create"])},
            {"address": "a.one", "change": _change(["create"])},
        )
        reverse = _plan(
            {"address": "a.one", "change": _change(["create"])},
            {"address": "b.two", "change": _change(["create"])},
        )

        self.assertEqual([item["address"] for item in project_plan(forward)], ["a.one", "b.two"])
        self.assertEqual(compute_plan_hash(forward), compute_plan_hash(reverse))

    def test_canonical_bytes_use_compact_ascii_separators(self) -> None:
        plan = _plan(
            {"address": "aws_s3_bucket.logs", "change": _change(["create"], after={"nÃme": "x"})}
        )
        raw = canonical_plan_bytes(plan)

        self.assertNotIn(b", ", raw)  # 구분자에 공백이 없다
        self.assertNotIn(b": ", raw)
        self.assertFalse(raw.endswith(b"\n"))  # trailing newline 없음
        raw.decode("ascii")  # 비-ASCII escape로 ASCII만 남는다

    def test_delete_action_is_destructive(self) -> None:
        plan = _plan({"address": "aws_s3_bucket.logs", "change": _change(["delete"])})

        self.assertTrue(has_destructive_changes(plan))

    def test_replace_paths_is_destructive(self) -> None:
        plan = _plan(
            {
                "address": "aws_s3_bucket.logs",
                "change": _change(["create", "delete"], replace_paths=[["bucket"]]),
            }
        )

        self.assertTrue(has_destructive_changes(plan))

    def test_pure_update_is_not_destructive(self) -> None:
        plan = _plan(
            {
                "address": "aws_s3_bucket.logs",
                "change": _change(["update"], replace_paths=[]),
            }
        )

        self.assertFalse(has_destructive_changes(plan))

    def test_non_finite_numbers_are_rejected(self) -> None:
        plan = _plan(
            {
                "address": "aws_s3_bucket.logs",
                "change": _change(["update"], after={"n": float("nan")}),
            }
        )

        with self.assertRaisesRegex(TerraformPlanProjectionError, "non-finite"):
            compute_plan_hash(plan)

    def test_missing_resource_changes_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(TerraformPlanProjectionError, "resource_changes"):
            project_plan({"format_version": "1.2"})

    def test_missing_actions_is_rejected(self) -> None:
        plan = _plan({"address": "aws_s3_bucket.logs", "change": {"before": {}, "after": {}}})

        with self.assertRaisesRegex(TerraformPlanProjectionError, "actions"):
            project_plan(plan)

    def test_duplicate_addresses_are_rejected(self) -> None:
        plan = _plan(
            {"address": "aws_s3_bucket.logs", "change": _change(["create"])},
            {"address": "aws_s3_bucket.logs", "change": _change(["update"])},
        )

        with self.assertRaisesRegex(TerraformPlanProjectionError, "unique"):
            project_plan(plan)

    def test_empty_plan_hashes_deterministically(self) -> None:
        empty = {"resource_changes": []}

        self.assertEqual(compute_plan_hash(empty), compute_plan_hash({"resource_changes": []}))
        self.assertFalse(has_destructive_changes(empty))


if __name__ == "__main__":
    unittest.main()
