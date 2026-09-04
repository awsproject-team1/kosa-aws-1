"""The Bedrock remediation agent keeps generated patches inside the snapshot boundary."""

import json
import unittest

from agent.runtime import IaCDocument, MockGitHubTool
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


COMMIT = "b283b6b5a41945349f64c41036870a5507c264f7"
INSECURE_MAIN_TF = (
    'resource "aws_s3_bucket" "sandbox" {\n'
    "  bucket = var.sandbox_bucket_name\n"
    "}\n"
    "\n"
    'resource "aws_s3_bucket_public_access_block" "sandbox" {\n'
    "  bucket = aws_s3_bucket.sandbox.id\n"
    "\n"
    "  block_public_acls       = false\n"
    "  block_public_policy     = false\n"
    "  ignore_public_acls      = false\n"
    "  restrict_public_buckets = false\n"
    "}\n"
)
#: 최소 변경: 네 플래그만 true로 바꾸고 나머지 바이트는 그대로다.
SECURE_MAIN_TF = INSECURE_MAIN_TF.replace("= false", "= true")
#: 리소스 블록 하나를 통째로 지운, 원본을 보지 않은 모델이 내는 형태의 응답.
REWRITTEN_MAIN_TF = (
    'resource "aws_s3_bucket_public_access_block" "sandbox" {\n'
    "  block_public_acls       = true\n"
    "  block_public_policy     = true\n"
    "  ignore_public_acls      = true\n"
    "  restrict_public_buckets = true\n"
    "}\n"
)


def iac_documents(files: tuple[tuple[str, str], ...] = (("main.tf", INSECURE_MAIN_TF),)):
    return MockGitHubTool(
        customer_id="kosa-sandbox",
        repository_id="test-s3-sandbox",
        snapshots=(),
        documents=(
            IaCDocument(
                customer_id="kosa-sandbox",
                repository_id="test-s3-sandbox",
                commit_sha=COMMIT,
                files=files,
            ),
        ),
    )


class BedrockPatchGeneratorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.content_store = InMemoryPatchContentStore()

    def generator(self, client: Client, documents=None) -> BedrockPatchGenerator:
        return BedrockPatchGenerator(
            client=client,
            model_profile=REMEDIATION_PROFILE,
            content_store=self.content_store,
            iac_documents=iac_documents() if documents is None else documents,
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
                client=Client({}),
                model_profile=REMEDIATION_PROFILE,
                content_store=None,
                iac_documents=iac_documents(),
            )

    def test_the_model_sees_the_terraform_body_of_the_assessed_commit(self) -> None:
        """원본 없이 만든 patch는 검증할 수 없다. 모델 입력에 파일 본문이 있어야 한다."""
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        self.generator(client).generate(context=context(), decision=decision())
        body = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        self.assertEqual(
            body["terraform_files"], [{"path": "main.tf", "content": INSECURE_MAIN_TF}]
        )
        self.assertIn("terraform_files", client.calls[0]["system"][0]["text"])

    def test_the_model_sees_the_control_and_the_attributes_the_plan_check_will_read(self) -> None:
        """rationale만으로는 AWS 경로를 Terraform attribute로 혼자 번역해야 했다. 매핑은 Catalog에 있다."""
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        self.generator(client).generate(context=context(), decision=decision())
        body = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        guidance = body["remediation_guidance"]
        self.assertEqual(guidance["control"]["control_key"], "S3_BLOCK_PUBLIC_ACCESS")
        self.assertEqual(
            {check["attribute_path"] for check in guidance["plan_checks"]},
            {
                "block_public_acls",
                "ignore_public_acls",
                "block_public_policy",
                "restrict_public_buckets",
            },
        )
        self.assertTrue(
            all(
                check["terraform_resource_type"] == "aws_s3_bucket_public_access_block"
                and check["expectation"] == "ALL_TRUE"
                for check in guidance["plan_checks"]
            )
        )
        self.assertIn("plan_checks", client.calls[0]["system"][0]["text"])
        # legacy Rule에는 승인된 rubric이 없다. 없는 것을 지어내지 않는다.
        self.assertNotIn("evaluation_rubric", guidance)

    def test_an_approved_rule_lends_its_rubric_to_the_guidance(self) -> None:
        from packages.contracts import (
            AssessmentPhase,
            PolicyRule,
            RuleEvaluationType,
            RuleSeverity,
            SourceReference,
        )

        rule = PolicyRule(
            rule_id="S3-PUBLIC-001",
            version="2026-08-31",
            title="Buckets block public access",
            severity=RuleSeverity.CRITICAL,
            applicable_phases=(AssessmentPhase.INITIAL,),
            resource_types=("AWS::S3::Bucket",),
            source_references=(
                SourceReference(
                    source_id="p", source_version="v1", locator="p#1", content_sha256="x"
                ),
            ),
            control_key="S3_BLOCK_PUBLIC_ACCESS",
            control_catalog_version="governance-control-catalog/2026-09-05",
            evaluation_type=RuleEvaluationType.AWS,
            required_evidence=("S3.PUBLIC_ACCESS_BLOCK",),
            evaluation_rubric="Fail when any block-public-access flag is false.",
        )
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        generator = BedrockPatchGenerator(
            client=client,
            model_profile=REMEDIATION_PROFILE,
            content_store=self.content_store,
            iac_documents=iac_documents(),
            rule_lookup=lambda rule_id, version: rule,
        )
        generator.generate(context=context(), decision=decision())
        body = json.loads(client.calls[0]["messages"][0]["content"][0]["text"])
        self.assertEqual(
            body["remediation_guidance"]["evaluation_rubric"],
            "Fail when any block-public-access flag is false.",
        )

    def test_refuses_to_run_without_a_terraform_source_reader(self) -> None:
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        generator = BedrockPatchGenerator(
            client=client,
            model_profile=REMEDIATION_PROFILE,
            content_store=self.content_store,
            iac_documents=None,
        )
        with self.assertRaisesRegex(BedrockPatchError, "source reader is not configured"):
            generator.generate(context=context(), decision=decision())
        self.assertEqual(client.calls, [])

    def test_rejects_a_file_the_snapshot_does_not_contain(self) -> None:
        client = Client({"changes": {"new.tf": SECURE_MAIN_TF}})
        with self.assertRaisesRegex(BedrockPatchError, "not a Terraform file"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_a_change_that_does_not_alter_the_file(self) -> None:
        client = Client({"changes": {"main.tf": INSECURE_MAIN_TF}})
        with self.assertRaisesRegex(BedrockPatchError, "does not alter"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_a_rewrite_that_drops_an_existing_resource_block(self) -> None:
        """원본을 보지 않은 모델이 내던 형태 — 버킷 리소스가 사라진 파일 — 를 막는다."""
        client = Client({"changes": {"main.tf": REWRITTEN_MAIN_TF}})
        with self.assertRaisesRegex(BedrockPatchError, "removes or renames resource blocks"):
            self.generator(client).generate(context=context(), decision=decision())

    def test_rejects_a_document_from_another_commit(self) -> None:
        other = MockGitHubTool(
            customer_id="kosa-sandbox",
            repository_id="test-s3-sandbox",
            snapshots=(),
            documents=(
                IaCDocument(
                    customer_id="kosa-sandbox",
                    repository_id="test-s3-sandbox",
                    commit_sha="c" * 40,
                    files=(("main.tf", INSECURE_MAIN_TF),),
                ),
            ),
        )
        client = Client({"changes": {"main.tf": SECURE_MAIN_TF}})
        from agent.runtime import GitHubSnapshotNotFoundError

        with self.assertRaises(GitHubSnapshotNotFoundError):
            self.generator(client, other).generate(context=context(), decision=decision())
        self.assertEqual(client.calls, [])

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
                iac_documents=iac_documents(),
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
