"""Verify the policy digest tool distinguishes source versions.

로컬 정책 원문이 없는 환경에서도 동작하도록, 원문을 읽지 않는 registry 파싱 경계만 검증한다.
"""

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "policy_source_digest.py"


def _load_module():
    """Load the script as a module without putting `scripts/` on `sys.path`."""
    spec = importlib.util.spec_from_file_location("policy_source_digest", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


digest_tool = _load_module()


def _write_registry(directory: Path, sources: list[dict[str, object]]) -> None:
    """Write a minimal registry that pins one rule per declared source version."""
    (directory / "sources.json").write_text(json.dumps(sources), encoding="utf-8")
    (directory / "profiles.json").write_text("[]", encoding="utf-8")
    (directory / "controls.json").write_text("[]", encoding="utf-8")
    rules = [
        {
            "rule_id": f"RULE-{index}",
            "version": "2026-08-31",
            "title": "rule",
            "severity": "HIGH",
            "applicable_phases": ["INITIAL"],
            "resource_types": ["AWS::S3::Bucket"],
            "source_references": [
                {
                    "source_id": source["source_id"],
                    "source_version": source["version"],
                    "locator": "control/2.6.2",
                    "content_sha256": str(index) * 64,
                }
            ],
        }
        for index, source in enumerate(sources)
    ]
    (directory / "rules.s3.json").write_text(json.dumps(rules), encoding="utf-8")


class RegistryReferenceKeyTest(unittest.TestCase):
    def setUp(self) -> None:
        self._original_registry_dirs = digest_tool.REGISTRY_DIRS

    def tearDown(self) -> None:
        digest_tool.REGISTRY_DIRS = self._original_registry_dirs

    def test_keeps_two_versions_of_the_same_source_apart(self) -> None:
        """같은 Source의 구·신 version이 공존해도 한쪽이 덮어써지면 안 된다."""
        with TemporaryDirectory() as name:
            directory = Path(name)
            _write_registry(
                directory,
                [
                    {
                        "source_id": "isms-p-2023",
                        "kind": "ISMS_P",
                        "title": "old",
                        "version": "2023-10-31",
                        "artifact_id": "art-old",
                        "content_sha256": "a" * 64,
                    },
                    {
                        "source_id": "isms-p-2023",
                        "kind": "ISMS_P",
                        "title": "new",
                        "version": "2026-01-01",
                        "artifact_id": "art-new",
                        "content_sha256": "b" * 64,
                    },
                ],
            )
            digest_tool.REGISTRY_DIRS = (directory,)

            source_digests, references = digest_tool._registry_references()

            self.assertEqual(
                set(source_digests),
                {("isms-p-2023", "2023-10-31"), ("isms-p-2023", "2026-01-01")},
            )
            self.assertEqual(source_digests[("isms-p-2023", "2023-10-31")], "a" * 64)
            self.assertEqual(source_digests[("isms-p-2023", "2026-01-01")], "b" * 64)
            self.assertEqual(
                {(source_id, version) for source_id, version, _, _ in references},
                {("isms-p-2023", "2023-10-31"), ("isms-p-2023", "2026-01-01")},
            )

    def test_rejects_a_duplicate_source_version(self) -> None:
        with TemporaryDirectory() as name:
            directory = Path(name)
            source = {
                "source_id": "isms-p-2023",
                "kind": "ISMS_P",
                "title": "duplicate",
                "version": "2023-10-31",
                "artifact_id": "art-dup",
                "content_sha256": "a" * 64,
            }
            _write_registry(directory, [source, dict(source)])
            digest_tool.REGISTRY_DIRS = (directory,)

            with self.assertRaisesRegex(ValueError, "duplicate policy source"):
                digest_tool._registry_references()


class SourceMappingTest(unittest.TestCase):
    def test_unknown_source_version_names_the_pinned_version(self) -> None:
        with self.assertRaisesRegex(digest_tool.UnknownPolicySourceError, "isms-p-2023@1999-01-01"):
            digest_tool._source_path("isms-p-2023", "1999-01-01")

    def test_every_registry_source_version_has_a_local_original_mapping(self) -> None:
        """Registry가 선언한 모든 Source version은 원문 파일 매핑을 가져야 한다."""
        sources = json.loads(
            (REPO_ROOT / "fixtures" / "rules" / "sources.json").read_text(encoding="utf-8")
        )

        for source in sources:
            self.assertIn((source["source_id"], source["version"]), digest_tool.SOURCE_FILES)


if __name__ == "__main__":
    unittest.main()
