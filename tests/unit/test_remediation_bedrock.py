"""The Bedrock remediation agent keeps generated patches inside the snapshot boundary."""

import json
import unittest

from apps.backend.remediation.bedrock import BedrockPatchError, BedrockPatchGenerator
from apps.backend.remediation.patch_content import InMemoryPatchContentStore
from packages.contracts import (
    ArtifactReference,
    ArtifactType,
    EvaluationPerspective,
    EvaluationStatus,
    Finding,
    IaCSnapshot,
    ModelProfile,
    ModelProfileRole,
    RemediationAction,
    RemediationContext,
    RemediationDecision,
)

REMEDIATION_PROFILE = ModelProfile(
    model_profile_id="remediation-nova-lite-m1-v1",
    role=ModelProfileRole.REMEDIATION,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="remediation-v1",
    rubric_version="remediation-v1",
    golden_dataset_version="remediation-v1",
)
ASSESSMENT_PROFILE = ModelProfile(
    model_profile_id="assessment-nova-lite-m1-v1",
    role=ModelProfileRole.ASSESSMENT,
    region="us-east-1",
    model_id="amazon.nova-lite-v1:0",
    prompt_version="assessment-v1",
    rubric_version="mvp-v1",
    golden_dataset_version="m1-s3-v1",
)


def context() -> RemediationContext:
    finding = Finding(
        finding_id="finding-abc",
        resource_id="tfsbx-bucket",
        rule_id="S3-PUBLIC-001",
        rule_version="2026-08-31",
        perspective=EvaluationPerspective.IAC,
        status=EvaluationStatus.FAIL,
        severity="CRITICAL",
        score=0,
        rationale="public access block flags are disabled",
        evidence_references=("terraform:main.tf",),
        assessed_commit_sha="b283b6b5a41945349f64c41036870a5507c264f7",
        evaluated_at="2026-09-03T07:38:41+00:00",
    )
    return RemediationContext(
        finding=finding,
        snapshot=IaCSnapshot(
            customer_id="kosa-sandbox",
            repository_id="test-s3-sandbox",
            commit_sha="b283b6b5a41945349f64c41036870a5507c264f7",
            artifact=ArtifactReference(
                artifact_id="terraform-snapshot:test-s3-sandbox:b283b6b",
                artifact_type=ArtifactType.TERRAFORM_SNAPSHOT,
                content_sha256="a" * 64,
                customer_id="kosa-sandbox",
                repository_id="test-s3-sandbox",
            ),
        ),
        evidence_references=("terraform:main.tf",),
    )


def decision() -> RemediationDecision:
    f = context().finding
    return RemediationDecision(
        finding_id=f.finding_id,
        resource_id=f.resource_id,
        rule_id=f.rule_id,
        rule_version=f.rule_version,
        perspective=f.perspective,
        action=RemediationAction.TERRAFORM_PATCH,
    )


class Client:
    def __init__(self, body: object) -> None:
        self.body = body
        self.calls: list[dict[str, object]] = []

    def converse(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": json.dumps(self.body)}]}}}


SECURE_MAIN_TF = (
    'resource "aws_s3_bucket_public_access_block" "sandbox" {\n'
    "  block_public_acls       = true\n"
    "  block_public_policy     = true\n"
    "  ignore_public_acls      = true\n"
    "  restrict_public_buckets = true\n"
    "}\n"
)


class BedrockPatchGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content_store = InMemoryPatchContentStore()

    def generator(self, client: Client) -> BedrockPatchGenerator:
        return BedrockPatchGenerator(
            client=client, model_profile=REMEDIATION_PROFILE, content_store=self.content_store
        )

    def test_stores_the_patch_bytes_under_the_patch_digest(self) -> None:
        """digest만 남기고 내용을 버리면 PR write는 만들 것이 없다."""
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        patch = self.generator(client).generate(context=context(), decision=decision())
        stored = self.content_store.get(patch=patch)
        self.assertEqual(stored.changes, {"main.tf": SECURE_MAIN_TF})
        self.assertEqual(stored.finding_id, patch.finding_id)
        self.assertEqual(stored.base_commit_sha, patch.base_commit_sha)

    def test_requires_a_content_store(self) -> None:
        with self.assertRaises(TypeError):
            BedrockPatchGenerator(
                client=Client({}), model_profile=REMEDIATION_PROFILE, content_store=None
            )

    def test_binds_generated_patch_to_the_snapshot_and_finding(self) -> None:
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})

        patch = self.generator(client).generate(context=context(), decision=decision())

        self.assertEqual(patch.finding_id, "finding-abc")
        self.assertEqual(patch.base_commit_sha, context().snapshot.commit_sha)
        self.assertEqual(patch.artifact.artifact_type, ArtifactType.REMEDIATION_PATCH)
        self.assertEqual(patch.artifact.customer_id, "kosa-sandbox")
        self.assertEqual(patch.artifact.repository_id, "test-s3-sandbox")
        self.assertEqual(patch.changed_paths, ("main.tf",))
        self.assertEqual(client.calls[0]["modelId"], REMEDIATION_PROFILE.model_id)

    def test_is_deterministic_for_identical_changes(self) -> None:
        body = {"changes": {"main.tf": SECURE_MAIN_TF}}
        first = self.generator(Client(body)).generate(context=context(), decision=decision())
        second = self.generator(Client(body)).generate(context=context(), decision=decision())
        self.assertEqual(first.artifact.content_sha256, second.artifact.content_sha256)

    def test_rejects_a_non_remediation_model_profile(self) -> None:
        with self.assertRaisesRegex(BedrockPatchError, "not approved for remediation"):
            BedrockPatchGenerator(
                client=Client({}),
                model_profile=ASSESSMENT_PROFILE,
                content_store=InMemoryPatchContentStore(),
            )

    def test_rejects_paths_outside_the_repository(self) -> None:
        client = Client({"changes": {"../outside.tf": SECURE_MAIN_TF}})
        with self.assertRaisesRegex(BedrockPatchError, "outside the repository"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_absolute_paths(self) -> None:
        client = Client({"changes": {"/etc/evil.tf": SECURE_MAIN_TF}})
        with self.assertRaisesRegex(BedrockPatchError, "outside the repository"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_empty_change_set(self) -> None:
        client = Client({"changes": {}})
        with self.assertRaisesRegex(BedrockPatchError, "non-empty object"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_extra_response_fields(self) -> None:
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}, "commit": "x"})
        with self.assertRaisesRegex(BedrockPatchError, "fields are invalid"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_accepts_a_code_fenced_object(self) -> None:
        fenced = "```json\n" + json.dumps({"changes": {"main.tf": SECURE_MAIN_TF}}) + "\n```"
        client = Client(None)
        client.body = None

        class Fenced(Client):
            def converse(self, **kwargs):
                self.calls.append(kwargs)
                return {"output": {"message": {"content": [{"text": fenced}]}}}

        patch = self.generator(Fenced(None)).generate(context=context(), decision=decision())
        self.assertEqual(patch.changed_paths, ("main.tf",))


if __name__ == "__main__":
    unittest.main()
