"""The publication wire shape: many sources, an optional baseline, and the old single form.

한 Profile이 사내 문서 여러 건과 ISMS-P 기준선을 함께 담게 되면서 body가 바뀌었다. 여기서
고정하는 것은 세 가지다.

1. **예전 형태(`source_id`/`source_version` 한 쌍)는 계속 받는다.** 이미 그 형태로 게시하는
   스크립트가 있고, 문서 하나짜리 게시는 새 형태에서도 의미가 같다.
2. **두 형태를 섞으면 거부한다.** 어느 쪽이 의도인지 알 수 없는 요청을 조용히 한쪽으로 해석하면,
   나머지 한쪽에 적힌 문서가 Profile에서 빠진 채로 게시가 성공한다.
3. **모르는 필드는 거부한다.** 오타 난 `baselines`가 무시되면 기준선 없는 Profile이 게시되고,
   사용자는 ISMS-P를 포함했다고 믿는다.
"""

import json
import unittest

from apps.backend.api.handler import _policy_profile_request


def _body(**fields: object) -> str:
    return json.dumps({"policy_profile_id": "profile-combined", "version": "v1", **fields})


class PolicyProfileRequestTest(unittest.TestCase):
    def test_a_list_of_sources_becomes_pairs(self) -> None:
        request = _policy_profile_request(
            _body(
                sources=[
                    {"source_id": "src-1", "source_version": "ver-1"},
                    {"source_id": "src-2", "source_version": "ver-2"},
                ]
            )
        )

        self.assertEqual(request["sources"], (("src-1", "ver-1"), ("src-2", "ver-2")))
        self.assertIsNone(request["baseline"])

    def test_the_single_source_form_still_publishes(self) -> None:
        request = _policy_profile_request(_body(source_id="src-1", source_version="ver-1"))

        self.assertEqual(request["sources"], (("src-1", "ver-1"),))

    def test_mixing_the_two_forms_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _policy_profile_request(
                _body(
                    source_id="src-1",
                    source_version="ver-1",
                    sources=[{"source_id": "src-2", "source_version": "ver-2"}],
                )
            )

    def test_a_baseline_is_read_as_a_profile_version_pair(self) -> None:
        request = _policy_profile_request(
            _body(
                sources=[],
                baseline={"policy_profile_id": "profile-multiresource-baseline", "version": "v1"},
            )
        )

        self.assertEqual(request["baseline"], ("profile-multiresource-baseline", "v1"))
        self.assertEqual(request["sources"], ())

    def test_an_unknown_field_is_refused_rather_than_ignored(self) -> None:
        with self.assertRaises(ValueError):
            _policy_profile_request(_body(baselines={"policy_profile_id": "x", "version": "v1"}))

    def test_a_baseline_missing_its_version_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            _policy_profile_request(
                _body(baseline={"policy_profile_id": "profile-multiresource-baseline"})
            )

    def test_the_replaced_version_may_be_pinned(self) -> None:
        """동시에 게시된 두 Profile 중 나중 것이 앞의 것을 조용히 덮어쓰지 않게 한다."""
        request = _policy_profile_request(
            _body(source_id="src-1", source_version="ver-1", expected_current_version="v1")
        )

        self.assertEqual(request["expected_current_version"], "v1")


if __name__ == "__main__":
    unittest.main()
