"""S3 Actual-state evidence adapter over D's read-only AWS Resource Tool."""

from __future__ import annotations

from dataclasses import dataclass

from agent.runtime import AwsResourceTool, AwsResourceView
from packages.contracts import AwsResourceOperation, AwsResourceQuery, EvaluationPerspective


class S3EvidenceError(ValueError):
    """Raised when a read-only AWS response is outside the requested S3 scope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class S3ActualEvidence:
    """Bounded S3 Actual evidence safe to supply to the Assessment evaluator."""

    resource_id: str
    resource_document: dict[str, object]
    evidence_references: tuple[str, ...]
    perspective: EvaluationPerspective = EvaluationPerspective.AWS_ACTUAL

    def __post_init__(self) -> None:
        if not isinstance(self.resource_id, str) or not self.resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        if not isinstance(self.resource_document, dict):
            raise TypeError("resource_document must be a dict")
        if not self.evidence_references or not all(
            isinstance(reference, str) and reference for reference in self.evidence_references
        ):
            raise ValueError("evidence_references must contain non-empty strings")
        if self.perspective is not EvaluationPerspective.AWS_ACTUAL:
            raise ValueError("S3 Actual evidence must use AWS_ACTUAL perspective")


class S3ActualEvidenceLoader:
    """Load one S3 bucket only through the approved READ_RESOURCE contract."""

    def __init__(self, *, tool: AwsResourceTool, customer_id: str, aws_account_id: str) -> None:
        if not isinstance(tool, AwsResourceTool):
            raise TypeError("tool must implement AwsResourceTool")
        for field_name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        self._tool = tool
        self._customer_id = customer_id
        self._aws_account_id = aws_account_id

    def load(self, resource_id: str) -> S3ActualEvidence:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        query = AwsResourceQuery(
            customer_id=self._customer_id,
            aws_account_id=self._aws_account_id,
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type="AWS::S3::Bucket",
            resource_id=resource_id,
        )
        view = self._tool.read_resource(query)
        self._validate_view(view, resource_id)
        return S3ActualEvidence(
            resource_id=resource_id,
            resource_document={
                "resource_type": view.resource_type,
                "resource_id": view.resource_id,
                "attributes": view.to_dict()["attributes"],
            },
            evidence_references=(f"aws:s3:bucket/{resource_id}#read-resource",),
        )

    def _validate_view(self, view: object, resource_id: str) -> None:
        if not isinstance(view, AwsResourceView):
            raise S3EvidenceError("AWS Resource Tool returned an invalid view")
        if (
            view.aws_account_id != self._aws_account_id
            or view.resource_type != "AWS::S3::Bucket"
            or view.resource_id != resource_id
        ):
            raise S3EvidenceError("AWS Resource Tool returned a view outside the S3 query scope")
