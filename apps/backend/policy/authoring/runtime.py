"""AWS Lambda composition root for the Policy Authoring Worker.

API가 `POLICY_AUTHORING_QUEUE_URL`로 보낸 `PolicyAuthoringRequest`를 SQS event source로 소비해
한 source version의 후보 추출을 실행한다. Queue payload는 `customer_id`와 판본 식별자뿐이며,
정책 텍스트는 담기지 않는다 — worker가 보호된 정규화 artifact를 자기 권한으로 다시 읽는다.

책임 분리는 다른 Worker runtime과 같다.

- `parse_requests(event)`: SQS Records → `PolicyAuthoringRequest`. 순수 함수.
- `run_requests(event, ...)`: 파싱한 각 요청을 주입된 구성요소로 처리한다.
- `lambda_handler(event, context)`: 실행체를 조립해 구동한다.

**`requested_at`은 payload가 정한다.** worker가 지금 시각을 쓰면 재시도마다 provenance가 달라져,
저장 계층이 같은 실행의 재시도를 다른 추출로 보고 fail-closed한다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping

from apps.backend.policy.authoring.artifact_reader import NormalizedArtifactReader
from apps.backend.policy.authoring.bedrock_extractor import BedrockPolicyCandidateExtractor
from apps.backend.policy.authoring.extractor import PolicyCandidateExtractor
from apps.backend.policy.authoring.pipeline import extract_policy_candidates
from apps.backend.policy.control_catalog import MVP_CONTROL_CATALOG
from packages.contracts import (
    AuthoringManifest,
    GovernanceControlCatalog,
    ModelProfile,
    ModelProfileRole,
    NormalizedPolicyDocument,
    PolicyAuthoringRequest,
    PolicyAuthoringResult,
)


class PolicyAuthoringRuntimeError(RuntimeError):
    """Authoring Worker runtime이 설정되지 않았거나 실행할 수 없다."""


def lambda_handler(event: Mapping[str, object], context: object) -> None:
    """SQS event source entrypoint."""
    documents, repository, extractor = _live_components()
    run_requests(
        event,
        documents=documents,
        repository=repository,
        extractor=extractor,
        artifact_reader=_live_artifact_reader(),
        catalog=MVP_CONTROL_CATALOG,
    )


def run_requests(
    event: Mapping[str, object],
    *,
    documents: object,
    repository: object,
    extractor: PolicyCandidateExtractor,
    artifact_reader: NormalizedArtifactReader,
    catalog: GovernanceControlCatalog,
) -> tuple[AuthoringManifest, ...]:
    """Extract and persist candidates for every request in this batch."""
    manifests: list[AuthoringManifest] = []
    for request in parse_requests(event):
        document = documents.get_document(  # type: ignore[attr-defined]
            customer_id=request.customer_id,
            source_id=request.source_id,
            source_version=request.source_version,
        )
        if not isinstance(document, NormalizedPolicyDocument):
            raise PolicyAuthoringRuntimeError("policy ingestion record is invalid")
        result = extract_policy_candidates(
            customer_id=request.customer_id,
            document=document,
            artifact_reader=artifact_reader,
            extractor=extractor,
            catalog=catalog,
            authoring_run_id=request.authoring_run_id,
            # 최초 요청 시각을 그대로 쓴다. 지금 시각을 쓰면 재시도가 다른 추출이 된다.
            requested_at=request.requested_at,
        )
        manifests.append(_persist(repository, request.customer_id, result))
    return tuple(manifests)


def _persist(
    repository: object, customer_id: str, result: PolicyAuthoringResult
) -> AuthoringManifest:
    manifest = repository.record_authoring_result(  # type: ignore[attr-defined]
        customer_id=customer_id, result=result
    )
    if not isinstance(manifest, AuthoringManifest):  # pragma: no cover - repository contract
        raise PolicyAuthoringRuntimeError("authoring persistence returned an invalid manifest")
    return manifest


def parse_requests(event: Mapping[str, object]) -> tuple[PolicyAuthoringRequest, ...]:
    """SQS Records를 `PolicyAuthoringRequest`로 파싱한다.

    다른 Worker의 `WorkflowTask`가 이 큐로 잘못 흘러들면 여기서 막는다. 큐를 잘못 지목한 것은
    재시도로 나아지지 않는다.
    """
    records = event.get("Records")
    if not isinstance(records, list):
        raise ValueError("SQS Records are required")
    requests: list[PolicyAuthoringRequest] = []
    for record in records:
        if not isinstance(record, Mapping):
            raise ValueError("SQS record is invalid")
        body = record.get("body")
        if not isinstance(body, str):
            raise ValueError("SQS record body is invalid")
        payload = json.loads(body)
        if not isinstance(payload, Mapping) or set(payload) != {
            "customer_id",
            "source_id",
            "source_version",
            "authoring_run_id",
            "requested_at",
        }:
            raise ValueError("policy authoring request body is invalid")
        requests.append(
            PolicyAuthoringRequest(
                customer_id=_string(payload.get("customer_id")),
                source_id=_string(payload.get("source_id")),
                source_version=_string(payload.get("source_version")),
                authoring_run_id=_string(payload.get("authoring_run_id")),
                requested_at=_string(payload.get("requested_at")),
            )
        )
    return tuple(requests)


def _live_components() -> tuple[object, object, PolicyCandidateExtractor]:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover - boto3 is provided by the Lambda runtime
        raise PolicyAuthoringRuntimeError("AWS Lambda boto3 runtime is required") from error

    from apps.backend.repositories.policy_approval import DynamoDbPolicyApprovalRepository
    from apps.backend.repositories.policy_ingestion import (
        DynamoDbPolicySourceUploadRepository,
    )

    table_name = _required_environment("METADATA_TABLE_NAME")
    bucket = _required_environment("POLICY_SOURCE_BUCKET_NAME")
    table = boto3.resource("dynamodb").Table(table_name)
    documents = DynamoDbPolicySourceUploadRepository(
        table=table, bucket=bucket, presigner=boto3.client("s3")
    )
    repository = DynamoDbPolicyApprovalRepository(
        table_name=table_name,
        transaction_client=boto3.client("dynamodb"),
        table=table,
    )
    return documents, repository, _live_extractor(boto3)


def _live_extractor(boto3: object) -> PolicyCandidateExtractor:
    profile = _model_profile()
    client = boto3.client(  # type: ignore[attr-defined]
        "bedrock-runtime", region_name=profile.region
    )
    return BedrockPolicyCandidateExtractor(client=client, model_profile=profile)


def _live_artifact_reader() -> NormalizedArtifactReader:
    try:
        import boto3
    except ImportError as error:  # pragma: no cover
        raise PolicyAuthoringRuntimeError("AWS Lambda boto3 runtime is required") from error
    return NormalizedArtifactReader(
        reader=boto3.client("s3"), bucket=_required_environment("POLICY_SOURCE_BUCKET_NAME")
    )


def _model_profile() -> ModelProfile:
    """Read the approved POLICY_AUTHORING profile from the deployment configuration.

    역할을 확인한다. Assessment용으로 승인된 profile을 여기에 설정하면, 정책 추출이 검토되지
    않은 prompt와 모델로 실행된다.
    """
    raw = _required_environment("POLICY_AUTHORING_MODEL_PROFILE_JSON")
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as error:
        raise PolicyAuthoringRuntimeError(
            "POLICY_AUTHORING_MODEL_PROFILE_JSON is invalid JSON"
        ) from error
    if not isinstance(values, Mapping):
        raise PolicyAuthoringRuntimeError("POLICY_AUTHORING_MODEL_PROFILE_JSON must be an object")
    try:
        profile = ModelProfile(
            model_profile_id=_string(values.get("model_profile_id")),
            role=ModelProfileRole(_string(values.get("role"))),
            region=_string(values.get("region")),
            model_id=_string(values.get("model_id")),
            prompt_version=_string(values.get("prompt_version")),
            rubric_version=_string(values.get("rubric_version")),
            golden_dataset_version=_string(values.get("golden_dataset_version")),
        )
    except (TypeError, ValueError) as error:
        raise PolicyAuthoringRuntimeError(
            "POLICY_AUTHORING_MODEL_PROFILE_JSON is invalid"
        ) from error
    if profile.role is not ModelProfileRole.POLICY_AUTHORING:
        raise PolicyAuthoringRuntimeError("model profile is not approved for policy authoring")
    return profile


def _required_environment(name: str) -> str:
    value = os.environ.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PolicyAuthoringRuntimeError(f"{name} is required")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("required value must be a non-empty string")
    return value
