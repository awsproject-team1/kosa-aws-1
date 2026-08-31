"""M1 S3 Actual evaluator composed from read-only evidence and Bedrock output guards."""

from apps.backend.assessment.bedrock import BedrockConverseClient, BedrockStructuredEvaluator
from apps.backend.assessment.s3 import S3ActualEvidenceLoader
from apps.backend.policy import PolicyContext
from packages.contracts import EvaluationResult, ModelProfile, PolicyRule


class S3ActualBedrockEvaluator:
    """An AssessmentRunner-compatible evaluator for one S3 Actual Resource × Rule."""

    def __init__(
        self, *, evidence_loader: S3ActualEvidenceLoader, client: BedrockConverseClient
    ) -> None:
        if not isinstance(evidence_loader, S3ActualEvidenceLoader):
            raise TypeError("evidence_loader must be an S3ActualEvidenceLoader")
        if client is None:
            raise TypeError("client is required")
        self._evidence_loader = evidence_loader
        self._client = client

    def evaluate(
        self,
        *,
        resource_id: str,
        rule: PolicyRule,
        context: PolicyContext,
        model_profile: ModelProfile,
    ) -> EvaluationResult:
        evidence = self._evidence_loader.load(resource_id)
        return BedrockStructuredEvaluator(
            client=self._client,
            perspective=evidence.perspective,
            resource_document=evidence.resource_document,
            evidence_references=evidence.evidence_references,
        ).evaluate(
            resource_id=resource_id,
            rule=rule,
            context=context,
            model_profile=model_profile,
        )
