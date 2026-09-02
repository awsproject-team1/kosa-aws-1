"""Assessment worker boundary."""

from apps.backend.assessment.bedrock import BedrockEvaluationError, BedrockStructuredEvaluator
from apps.backend.assessment.bedrock_runtime import BedrockConverseClientFactory
from apps.backend.assessment.comparison import (
    ComparisonAssessment,
    PlannedEvaluation,
    compare_post_deploy_assessments,
)
from apps.backend.assessment.coverage import AssessmentCoverage, calculate_coverage
from apps.backend.assessment.drift import DriftDerivationError, derive_drift_results
from apps.backend.assessment.findings import (
    DynamoDbFindingStore,
    FindingStoreError,
    ImmutableFindingConflict,
    finding_from_result,
)
from apps.backend.assessment.model_profiles import (
    InMemoryModelProfileRegistry,
    ModelProfileNotFoundError,
)
from apps.backend.assessment.models import Assessment
from apps.backend.assessment.quality import GoldenDatasetRunner, GoldenEvaluationReport
from apps.backend.assessment.readiness import calculate_readiness_score
from apps.backend.assessment.reporting import (
    AssessmentEvaluationPlan,
    AssessmentReport,
    AssessmentReportNotFoundError,
    AssessmentReportStoreError,
    DynamoDbAssessmentReportStore,
)
from apps.backend.assessment.results import (
    DynamoDbEvaluationResultStore,
    EvaluationResultStoreError,
    ImmutableEvaluationResultConflict,
)
from apps.backend.assessment.runner import AssessmentRunner, EvaluationContractError
from apps.backend.assessment.runtime_config import (
    M1AssessmentTarget,
    M1RuntimeConfiguration,
    M1RuntimeConfigurationError,
)
from apps.backend.assessment.s3 import S3ActualEvidence, S3ActualEvidenceLoader, S3EvidenceError
from apps.backend.assessment.s3_evaluator import S3ActualBedrockEvaluator
from apps.backend.assessment.verification import (
    VerificationAssessmentScope,
    VerificationRejectionCode,
    VerificationScopeError,
    VerificationSource,
    plan_verification_assessment,
)
from apps.backend.assessment.worker import (
    AssessmentPlanError,
    AssessmentResourceWork,
    AssessmentWorker,
    AssessmentWorkNotFoundError,
)

__all__ = [
    "Assessment",
    "AssessmentCoverage",
    "AssessmentEvaluationPlan",
    "AssessmentPlanError",
    "AssessmentReport",
    "AssessmentReportNotFoundError",
    "AssessmentReportStoreError",
    "AssessmentResourceWork",
    "AssessmentRunner",
    "AssessmentWorker",
    "AssessmentWorkNotFoundError",
    "BedrockEvaluationError",
    "BedrockConverseClientFactory",
    "BedrockStructuredEvaluator",
    "calculate_coverage",
    "compare_post_deploy_assessments",
    "ComparisonAssessment",
    "calculate_readiness_score",
    "derive_drift_results",
    "DriftDerivationError",
    "DynamoDbEvaluationResultStore",
    "DynamoDbAssessmentReportStore",
    "DynamoDbFindingStore",
    "EvaluationContractError",
    "EvaluationResultStoreError",
    "FindingStoreError",
    "finding_from_result",
    "GoldenDatasetRunner",
    "GoldenEvaluationReport",
    "InMemoryModelProfileRegistry",
    "M1AssessmentTarget",
    "M1RuntimeConfiguration",
    "M1RuntimeConfigurationError",
    "ImmutableEvaluationResultConflict",
    "ImmutableFindingConflict",
    "ModelProfileNotFoundError",
    "PlannedEvaluation",
    "plan_verification_assessment",
    "S3ActualEvidence",
    "S3ActualEvidenceLoader",
    "S3ActualBedrockEvaluator",
    "S3EvidenceError",
    "VerificationAssessmentScope",
    "VerificationRejectionCode",
    "VerificationScopeError",
    "VerificationSource",
]
