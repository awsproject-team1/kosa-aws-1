"""Evidence path lookup decides whether Runtime may call the model at all.

이 검사가 느슨하면 근거가 없는데도 모델을 부르고, 모델의 추측이 `INSUFFICIENT_EVIDENCE` 자리에
들어간다. 반대로 지나치게 엄격하면 실제로 수집된 근거를 없다고 판정해 정상 리소스가
평가되지 않는다. 그래서 "없음"의 정의를 여기서 못 박는다.
"""

import unittest

from apps.backend.policy.evidence_paths import (
    EvidencePathError,
    document_path_present,
    missing_document_paths,
)

DOCUMENT = {
    "resource_type": "AWS::EC2::Instance",
    "resource_id": "i-01",
    "attributes": {
        "instance": {"InstanceId": "i-01", "PublicIpAddress": "203.0.113.10"},
        "volumes": [
            {"VolumeId": "vol-01", "Encrypted": True},
            {"VolumeId": "vol-02", "Encrypted": False},
        ],
        "security_groups": [],
        "load_balancer_attributes": {"access_logs.s3.enabled": "true"},
        "reported_nothing": None,
    },
}


class PathLookupTest(unittest.TestCase):
    def test_a_plain_path_resolves_through_mappings(self) -> None:
        self.assertTrue(document_path_present(DOCUMENT, "attributes.instance.PublicIpAddress"))
        self.assertFalse(document_path_present(DOCUMENT, "attributes.instance.PrivateIpAddress"))

    def test_a_list_path_requires_every_element_to_carry_the_field(self) -> None:
        """원소 하나만 field를 가지면 그 리소스의 근거는 불완전하다.

        볼륨 두 개 중 하나만 암호화 상태를 보고했다면, 나머지 하나에 대해서는 판정할 근거가
        없다. 부분 응답을 통과시키면 "모두 암호화됨"과 구별되지 않는다.
        """
        self.assertTrue(document_path_present(DOCUMENT, "attributes.volumes[].Encrypted"))

        partial = {
            "attributes": {
                "volumes": [{"VolumeId": "vol-01", "Encrypted": True}, {"VolumeId": "vol-02"}]
            }
        }
        self.assertFalse(document_path_present(partial, "attributes.volumes[].Encrypted"))

    def test_an_empty_list_is_not_collected_evidence(self) -> None:
        self.assertFalse(document_path_present(DOCUMENT, "attributes.security_groups[].GroupId"))

    def test_an_explicit_null_counts_as_missing(self) -> None:
        """adapter는 응답이 보고하지 않은 field를 넣지 않는다. `None`은 근거가 아니다."""
        self.assertFalse(document_path_present(DOCUMENT, "attributes.reported_nothing"))

    def test_a_braced_segment_addresses_a_key_containing_dots(self) -> None:
        self.assertTrue(
            document_path_present(
                DOCUMENT, "attributes.load_balancer_attributes.{access_logs.s3.enabled}"
            )
        )
        self.assertFalse(
            document_path_present(
                DOCUMENT, "attributes.load_balancer_attributes.{access_logs.s3.bucket}"
            )
        )

    def test_a_string_is_not_traversed_as_a_list(self) -> None:
        self.assertFalse(document_path_present(DOCUMENT, "resource_id[].anything"))

    def test_missing_paths_are_reported_in_declaration_order(self) -> None:
        missing = missing_document_paths(
            DOCUMENT,
            (
                "attributes.instance.PublicIpAddress",
                "attributes.instance.PrivateIpAddress",
                "attributes.security_groups[].GroupId",
            ),
        )

        self.assertEqual(
            missing,
            ("attributes.instance.PrivateIpAddress", "attributes.security_groups[].GroupId"),
        )


class PathParsingTest(unittest.TestCase):
    def test_a_malformed_path_fails_at_declaration_time(self) -> None:
        """Catalog가 만들어지는 시점에 실패해야 한다.

        런타임까지 미루면 오타 난 경로는 영원한 "근거 없음"으로만 드러나고, 그것은 실제
        위반과 구별되지 않는다.
        """
        for path in ("", "  ", "attributes.", "attributes..instance", "attributes.{unclosed"):
            with self.subTest(path=path):
                with self.assertRaises(EvidencePathError):
                    document_path_present(DOCUMENT, path)


if __name__ == "__main__":
    unittest.main()
