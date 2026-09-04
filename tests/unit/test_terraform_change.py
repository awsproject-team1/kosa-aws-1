"""A remediation change must be a minimal edit of the snapshot's own Terraform files."""

import unittest

from agent.runtime import IaCDocument
from apps.backend.remediation.terraform_change import (
    TerraformChangeError,
    render_unified_diff,
    resource_block_headers,
    validate_terraform_changes,
)

COMMIT = "a" * 40
DATABASE_TF = (
    'resource "aws_db_subnet_group" "app" {\n'
    '  name       = "app-db"\n'
    "  subnet_ids = aws_subnet.private[*].id\n"
    "}\n"
    "\n"
    'resource "aws_db_instance" "app" {\n'
    '  identifier          = "app-db"\n'
    '  engine              = "mysql"\n'
    "  publicly_accessible = true\n"
    "  storage_encrypted   = false\n"
    "}\n"
)
NETWORK_TF = 'resource "aws_vpc" "main" {\n  cidr_block = "10.0.0.0/16"\n}\n'


def _document() -> IaCDocument:
    return IaCDocument(
        customer_id="cust-001",
        repository_id="repo-001",
        commit_sha=COMMIT,
        files=(("database.tf", DATABASE_TF), ("network.tf", NETWORK_TF)),
    )


class ResourceHeadersTest(unittest.TestCase):
    def test_lists_every_resource_block_in_order(self) -> None:
        self.assertEqual(
            resource_block_headers(DATABASE_TF),
            (("aws_db_subnet_group", "app"), ("aws_db_instance", "app")),
        )


class ValidateChangesTest(unittest.TestCase):
    def test_a_single_attribute_edit_passes(self) -> None:
        fixed = DATABASE_TF.replace("publicly_accessible = true", "publicly_accessible = false")
        validate_terraform_changes(_document(), {"database.tf": fixed})

    def test_a_new_file_is_refused(self) -> None:
        with self.assertRaisesRegex(TerraformChangeError, "not a Terraform file"):
            validate_terraform_changes(_document(), {"fix.tf": 'resource "x" "y" {}\n'})

    def test_an_unchanged_file_is_refused(self) -> None:
        with self.assertRaisesRegex(TerraformChangeError, "does not alter"):
            validate_terraform_changes(_document(), {"database.tf": DATABASE_TF})

    def test_dropping_a_resource_block_is_refused(self) -> None:
        """A rewrite that only keeps the DB instance would destroy the subnet group on apply."""
        rewritten = (
            'resource "aws_db_instance" "app" {\n'
            '  identifier          = "app-db"\n'
            "  publicly_accessible = false\n"
            "}\n"
        )
        with self.assertRaisesRegex(TerraformChangeError, "aws_db_subnet_group.app"):
            validate_terraform_changes(_document(), {"database.tf": rewritten})

    def test_renaming_a_resource_is_refused(self) -> None:
        renamed = DATABASE_TF.replace('"aws_db_instance" "app"', '"aws_db_instance" "database"')
        with self.assertRaisesRegex(TerraformChangeError, "aws_db_instance.app"):
            validate_terraform_changes(_document(), {"database.tf": renamed})

    def test_adding_a_resource_block_is_allowed(self) -> None:
        """A logging or encryption fix may legitimately add a block; only removal is refused."""
        added = DATABASE_TF + '\nresource "aws_kms_key" "db" {\n  enable_key_rotation = true\n}\n'
        validate_terraform_changes(_document(), {"database.tf": added})


class UnifiedDiffTest(unittest.TestCase):
    def test_renders_only_the_changed_lines(self) -> None:
        fixed = DATABASE_TF.replace("publicly_accessible = true", "publicly_accessible = false")
        diff = render_unified_diff(_document(), {"database.tf": fixed})
        self.assertIn("--- a/database.tf", diff)
        self.assertIn("+++ b/database.tf", diff)
        self.assertIn("-  publicly_accessible = true", diff)
        self.assertIn("+  publicly_accessible = false", diff)
        self.assertNotIn("network.tf", diff)
        removed = [
            line
            for line in diff.splitlines()
            if line.startswith("-") and not line.startswith("---")
        ]
        added = [
            line
            for line in diff.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        ]
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(added), 1)

    def test_a_change_to_an_unknown_path_is_refused(self) -> None:
        with self.assertRaises(TerraformChangeError):
            render_unified_diff(_document(), {"other.tf": "x\n"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
