"""M1 Actual evaluator composed from read-only evidence and Bedrock output guards."""

from apps.backend.assessment.actual import ActualEvidenceLoader
from apps.backend.assessment.bedrock import BedrockConverseClient, BedrockStructuredEvaluator
from apps.backend.policy import PolicyContext
from packages.contracts import EvaluationResult, ModelProfile, PolicyRule


class ActualBedrockEvaluator:
    """An AssessmentRunner-compatible evaluator for one Actual Resource × Rule.

    The resource type is fixed by the injected loader, so a runner that was given an EC2
    target cannot end up evaluating an S3 document.
    """

    def __init__(
        self, *, evidence_loader: ActualEvidenceLoader, client: BedrockConverseClient
    ) -> None:
        if not isinstance(evidence_loader, ActualEvidenceLoader):
            raise TypeError("evidence_loader must be an ActualEvidenceLoader")
        if client is None:
            raise TypeError("client is required")
        self._evidence_loader = evidence_loader
        self._client = client

    @property
    def resource_type(self) -> str:
        return self._evidence_loader.resource_type

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
