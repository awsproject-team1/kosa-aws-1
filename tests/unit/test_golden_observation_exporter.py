"""The A producer emits a bundle the C gate accepts, and never leaks customer material."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from apps.backend.assessment.golden_observations import (
    CUSTOMER_SANDBOX_RUNTIME_MODE,
    ArtifactStoreGoldenSnapshotReader,
    DirectoryGoldenSnapshotReader,
    GoldenExecutionIdentity,
    GoldenObservationError,
    GoldenObservationExporter,
    UsageRecordingConverseClient,
    stable_error_code,
)
from apps.backend.assessment.release_quality import (
    GoldenReleaseQualityError,
    evaluate_golden_release_quality,
    load_approved_model_profile,
    load_golden_observation_bundle,
    load_release_golden_cases,
)
from apps.backend.policy import load_rule_registry
from apps.backend.policy.demo import load_demo_policy_coverage
from apps.backend.repositories.ports import ArtifactReference

ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import export_golden_observations as cli  # noqa: E402

MANIFEST = load_demo_policy_coverage(ROOT / "fixtures" / "m4" / "demo_policy_coverage.json")
CASES = load_release_golden_cases(
    ROOT / "fixtures" / "m1" / "golden_dataset_post_deploy_cases.json", manifest=MANIFEST
)
PROFILE = load_approved_model_profile(ROOT / "fixtures" / "m1" / "assessment_model_profile.json")
REGISTRY = load_rule_registry(ROOT / "fixtures" / "rules")
POLICY_PROFILE = (MANIFEST.policy_profile_id, MANIFEST.policy_profile_version)
SECRET_RESOURCE = "arn:aws:s3:::customer-bucket-do-not-leak"
SECRET_MESSAGE = "Rate exceeded for account 123456789012"
SECRET_DOCUMENT_MARKER = "customer-document-do-not-leak"


def _write_snapshots(directory: Path, *, mismatch_rule: str | None = None) -> None:
    for entry in CASES:
        case = entry.case
        if case.perspective.value == "DRIFT":
            continue
        resource_id = f"{SECRET_RESOURCE}-{entry.rule_id}"
        if mismatch_rule == entry.rule_id and case.perspective.value == "AWS_ACTUAL":
            resource_id += "-other"
        (directory / f"{case.resource_snapshot_artifact_id}.json").write_text(
            json.dumps(
                {
                    "resource_id": resource_id,
                    "resource_document": {"attributes": {SECRET_DOCUMENT_MARKER: {}}},
                    "evidence_references": list(case.expected_evidence_references),
                }
            ),
            encoding="utf-8",
        )


class ProviderError(Exception):
    """Shaped like botocore's ClientError: a code plus a message that must not leak."""

    def __init__(self, code: str) -> None:
        super().__init__(SECRET_MESSAGE)
        self.response = {"Error": {"Code": code, "Message": SECRET_MESSAGE}}


class FakeBedrock:
    def __init__(self, *, fail_calls: set[int] = frozenset(), usage: bool = True) -> None:
        self._expected = cli.ExpectedOutcomeClient(CASES)
        self._fail_calls = set(fail_calls)
        self._usage = usage
        self.calls = 0

    def converse(self, **kwargs):
        self.calls += 1
        if self.calls in self._fail_calls:
            raise ProviderError("ThrottlingException")
        response = dict(self._expected.converse(**kwargs))
        if not self._usage:
            response.pop("usage")
        return response


def _identity(runtime_mode: str = CUSTOMER_SANDBOX_RUNTIME_MODE) -> GoldenExecutionIdentity:
    return GoldenExecutionIdentity(
        scenario_id=MANIFEST.scenario_id,
        runtime_mode=runtime_mode,
        platform_commit_sha="a" * 40,
        repository_commit_sha256="b" * 64,
        deployment_id_sha256="c" * 64,
        artifact_set_sha256="d" * 64,
        execution_id="execution-001",
    )


def _exporter(client, snapshots: Path, *, repetitions: int = 5) -> GoldenObservationExporter:
    return GoldenObservationExporter(
        client=client,
        profile=PROFILE,
        cases=CASES,
        rules=REGISTRY.rules,
        policy_profile=POLICY_PROFILE,
        snapshots=DirectoryGoldenSnapshotReader(snapshots),
        repetitions=repetitions,
    )


def _through_gate(bundle: dict):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "observations.json"
        path.write_text(json.dumps(bundle), encoding="utf-8")
        loaded = load_golden_observation_bundle(path)
    return evaluate_golden_release_quality(
        loaded, manifest=MANIFEST, cases=CASES, approved_model_profile=PROFILE
    )


class GoldenObservationExporterTest(unittest.TestCase):
    def test_full_export_passes_the_release_gate(self) -> None:
        client = FakeBedrock()
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory))
            exporter = _exporter(client, Path(directory))
            bundle = exporter.export(_identity(), generated_at=datetime(2026, 9, 3, tzinfo=UTC))
        report = _through_gate(bundle)

        self.assertTrue(report.passes)
        self.assertEqual(report.observation_count, 90)
        self.assertEqual(report.bedrock_call_count, 60)
        self.assertEqual(report.code_derived_count, 30)
        self.assertEqual(report.execution_errors, 0)
        self.assertEqual(exporter.bedrock_call_count, 60)
        self.assertEqual(client.calls, 60)

    def test_bundle_carries_identifiers_and_digests_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory))
            bundle = _exporter(FakeBedrock(), Path(directory), repetitions=1).export(_identity())
        serialized = json.dumps(bundle)

        for forbidden in (SECRET_RESOURCE, SECRET_DOCUMENT_MARKER, "rationale", "dry run"):
            with self.subTest(forbidden=forbidden):
                # 실패 메시지에 bundle 전문을 싣지 않는다.
                self.assertTrue(forbidden not in serialized, f"bundle leaks: {forbidden}")
        self.assertEqual({len(item["resource_id_sha256"]) for item in bundle["observations"]}, {64})

    def test_provider_failure_becomes_a_stable_code_and_the_gate_fails(self) -> None:
        client = FakeBedrock(fail_calls={3})
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory))
            bundle = _exporter(client, Path(directory)).export(_identity())
        serialized = json.dumps(bundle)
        failed = [item for item in bundle["observations"] if item["error_code"] is not None]

        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error_code"], "PROVIDER_THROTTLED")
        self.assertEqual(failed[0]["status"], "EXECUTION_ERROR")
        self.assertEqual(failed[0]["execution_kind"], "BEDROCK")
        self.assertIsNone(failed[0]["latency_ms"])
        self.assertIsNone(failed[0]["evaluation_output_sha256"])
        for forbidden in (SECRET_MESSAGE, "123456789012"):
            with self.subTest(forbidden=forbidden):
                self.assertTrue(forbidden not in serialized, f"bundle leaks: {forbidden}")
        drift = next(
            item
            for item in bundle["observations"]
            if item["perspective"] == "DRIFT"
            and item["rule_id"] == failed[0]["rule_id"]
            and item["run_number"] == failed[0]["run_number"]
        )
        # DRIFT propagates the failed side in code and still carries an output digest.
        self.assertEqual(drift["status"], "EXECUTION_ERROR")
        self.assertEqual(drift["execution_kind"], "CODE_DERIVED")
        self.assertIsNone(drift["error_code"])
        self.assertIsNotNone(drift["evaluation_output_sha256"])
        report = _through_gate(bundle)
        self.assertFalse(report.passes)
        self.assertGreaterEqual(report.execution_errors, 2)

    def test_mismatched_pair_fails_before_any_model_call(self) -> None:
        client = FakeBedrock()
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory), mismatch_rule="S3-PUBLIC-001")
            with self.assertRaisesRegex(GoldenObservationError, "different resources"):
                _exporter(client, Path(directory)).export(_identity())
        self.assertEqual(client.calls, 0)

    def test_missing_snapshot_fails_before_any_model_call(self) -> None:
        client = FakeBedrock()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(GoldenObservationError, "missing"):
                _exporter(client, Path(directory)).export(_identity())
        self.assertEqual(client.calls, 0)

    def test_response_without_usage_is_refused_not_recorded_as_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _write_snapshots(Path(directory))
            with self.assertRaisesRegex(GoldenObservationError, "usage"):
                _exporter(FakeBedrock(usage=False), Path(directory)).export(_identity())

    def test_usage_recorder_prefers_provider_latency(self) -> None:
        class Inner:
            def converse(self, **kwargs):
                return {
                    "output": {},
                    "usage": {"inputTokens": 7, "outputTokens": 3},
                    "metrics": {"latencyMs": 250},
                }

        recorder = UsageRecordingConverseClient(Inner())
        recorder.converse()
        self.assertEqual(recorder.last_usage.latency_ms, 250)
        self.assertEqual(recorder.last_usage.input_tokens, 7)
        self.assertEqual(recorder.last_usage.output_tokens, 3)

    def test_error_codes_are_stable_and_message_free(self) -> None:
        self.assertEqual(
            stable_error_code(ProviderError("ThrottlingException")), "PROVIDER_THROTTLED"
        )
        self.assertEqual(
            stable_error_code(ProviderError("ValidationException")), "PROVIDER_VALIDATIONEXCEPTION"
        )
        self.assertEqual(stable_error_code(RuntimeError(SECRET_MESSAGE)), "PROVIDER_ERROR")

    def test_identity_rejects_unknown_runtime_mode(self) -> None:
        with self.assertRaisesRegex(GoldenObservationError, "runtime_mode"):
            _identity("LOCAL_BENCH")


class ArtifactStoreSnapshotReaderTest(unittest.TestCase):
    def test_reads_through_the_content_addressed_store(self) -> None:
        content = json.dumps(
            {
                "resource_id": "bucket-1",
                "resource_document": {"a": 1},
                "evidence_references": ["aws:s3:bucket-1"],
            }
        ).encode("utf-8")
        import hashlib

        digest = hashlib.sha256(content).hexdigest()
        seen: list[ArtifactReference] = []

        class Store:
            def get(self, reference):
                seen.append(reference)
                return content

        reader = ArtifactStoreGoldenSnapshotReader(
            Store(),
            customer_id="cust-1",
            index={"art-1": f"sha256:{digest}"},
            reference_factory=ArtifactReference,
        )
        snapshot = reader.read("art-1")
        self.assertEqual(snapshot.resource_id, "bucket-1")
        self.assertEqual(snapshot.content_sha256, digest)
        self.assertEqual(seen[0].hex_digest, digest)
        with self.assertRaisesRegex(GoldenObservationError, "not in the index"):
            reader.read("art-2")

    def test_store_failure_does_not_leak_customer_content(self) -> None:
        class Store:
            def get(self, reference):
                raise RuntimeError("s3://customer-bucket/secret-key")

        reader = ArtifactStoreGoldenSnapshotReader(
            Store(),
            customer_id="cust-1",
            index={"art-1": "sha256:" + "0" * 64},
            reference_factory=ArtifactReference,
        )
        with self.assertRaises(GoldenObservationError) as raised:
            reader.read("art-1")
        self.assertNotIn("secret-key", str(raised.exception))


class ExportCliTest(unittest.TestCase):
    def test_dry_run_bundle_is_rejected_by_the_gate_by_design(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            snapshots = Path(directory) / "snapshots"
            snapshots.mkdir()
            _write_snapshots(snapshots)
            output = Path(directory) / "private" / "observations.json"
            stdout, stderr = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                code = cli.main(
                    [
                        "--snapshots",
                        str(snapshots),
                        "--output",
                        str(output),
                        "--repetitions",
                        "1",
                        "--platform-commit",
                        "a" * 40,
                    ]
                )
            self.assertEqual(code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            self.assertEqual(summary["runtime_mode"], "DRY_RUN")
            self.assertEqual(summary["observation_count"], 18)
            self.assertEqual(summary["bedrock_call_count"], 12)
            self.assertIn("rejects it by design", stderr.getvalue())
            with self.assertRaisesRegex(GoldenReleaseQualityError, "CUSTOMER_SANDBOX"):
                load_golden_observation_bundle(output)

    def test_customer_sandbox_requires_binding_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = cli.main(
                    [
                        "--customer-sandbox",
                        "--output",
                        str(Path(directory) / "observations.json"),
                    ]
                )
            self.assertEqual(code, 2)
            self.assertIn("--demo-commit-sha", stderr.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
