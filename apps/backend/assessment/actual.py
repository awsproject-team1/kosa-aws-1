"""Actual-state evidence adapter over D's read-only AWS Resource Tool.

One loader is bound to one resource type. The evaluator receives a `resource_id` and
nothing else, so the type has to be fixed when the loader is built — otherwise the
evidence document and the Rule set could describe different kinds of resource.

Evidence locators live in an allow-list per resource type rather than being derived from
the type string. `PolicyContext.allows_evidence()` admits the whole `aws:` namespace, so a
derived locator would always look valid even when it named a resource shape nobody
reviewed. Registering the namespace per type keeps the vocabulary reviewed.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.runtime import (
    ACTUAL_READ_RESOURCE_TYPES,
    ALB_RESOURCE_TYPE,
    EC2_INSTANCE_RESOURCE_TYPE,
    RDS_INSTANCE_RESOURCE_TYPE,
    S3_RESOURCE_TYPE,
    AwsResourceTool,
    AwsResourceView,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery, EvaluationPerspective


class ActualEvidenceError(ValueError):
    """Raised when a read-only AWS response is outside the requested resource scope."""


@dataclass(frozen=True, slots=True, kw_only=True)
class _ActualResourceScope:
    """The reviewed evidence vocabulary of one resource type."""

    #: Short label used in scope-violation messages.
    label: str
    #: `aws:` namespace prefix the evidence locator is built from.
    evidence_prefix: str


#: Locator namespace per resource type. The **set of types** is not decided here — it comes
#: from the read adapter registry (`ACTUAL_READ_RESOURCE_TYPES`), and the assertion below
#: refuses to import if the two ever disagree. A type that can be read but has no reviewed
#: evidence vocabulary, or vice versa, is a half-wired resource type.
_ACTUAL_RESOURCE_SCOPES: dict[str, _ActualResourceScope] = {
    S3_RESOURCE_TYPE: _ActualResourceScope(label="S3", evidence_prefix="aws:s3:bucket/"),
    EC2_INSTANCE_RESOURCE_TYPE: _ActualResourceScope(
        label="EC2", evidence_prefix="aws:ec2:instance/"
    ),
    RDS_INSTANCE_RESOURCE_TYPE: _ActualResourceScope(
        label="RDS", evidence_prefix="aws:rds:db-instance/"
    ),
    ALB_RESOURCE_TYPE: _ActualResourceScope(
        label="ALB", evidence_prefix="aws:elasticloadbalancing:"
    ),
}

if set(_ACTUAL_RESOURCE_SCOPES) != set(ACTUAL_READ_RESOURCE_TYPES):  # pragma: no cover
    raise ImportError("Actual evidence scopes and read adapters must cover the same resource types")

SUPPORTED_ACTUAL_RESOURCE_TYPES: tuple[str, ...] = ACTUAL_READ_RESOURCE_TYPES


def actual_evidence_reference(resource_type: str, resource_id: str) -> str:
    """Return the single evidence locator an Actual read of one resource produces.

    For an ALB the `resource_id` is already an ARN, so only its resource part is appended —
    otherwise the locator would restate the service, region, and account that the `aws:`
    namespace and the customer scope already fix, and the model would have to echo a
    130-character string back for the evidence to be accepted.
    """
    scope = _ACTUAL_RESOURCE_SCOPES.get(resource_type)
    if scope is None:
        raise ActualEvidenceError(f"resource type {resource_type!r} has no Actual evidence scope")
    if not isinstance(resource_id, str) or not resource_id.strip():
        raise ValueError("resource_id must be a non-empty string")
    identifier = _arn_resource(resource_id) if resource_type == ALB_RESOURCE_TYPE else resource_id
    return f"{scope.evidence_prefix}{identifier}#read-resource"


def _arn_resource(arn: str) -> str:
    """Return the `resource` part of an ARN (`arn:partition:service:region:account:resource`)."""
    parts = arn.split(":", 5)
    if len(parts) != 6 or parts[0] != "arn" or not parts[5]:
        raise ActualEvidenceError("load balancer resource_id must be a load balancer ARN")
    return parts[5]


@dataclass(frozen=True, slots=True, kw_only=True)
class ActualEvidence:
    """Bounded Actual evidence safe to supply to the Assessment evaluator."""

    resource_type: str
    resource_id: str
    resource_document: dict[str, object]
    evidence_references: tuple[str, ...]
    perspective: EvaluationPerspective = EvaluationPerspective.AWS_ACTUAL

    def __post_init__(self) -> None:
        for name in ("resource_type", "resource_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.resource_type not in _ACTUAL_RESOURCE_SCOPES:
            raise ActualEvidenceError(
                f"resource type {self.resource_type!r} has no Actual evidence scope"
            )
        if not isinstance(self.resource_document, dict):
            raise TypeError("resource_document must be a dict")
        if not self.evidence_references or not all(
            isinstance(reference, str) and reference for reference in self.evidence_references
        ):
            raise ValueError("evidence_references must contain non-empty strings")
        if self.perspective is not EvaluationPerspective.AWS_ACTUAL:
            raise ValueError("Actual evidence must use AWS_ACTUAL perspective")


class ActualEvidenceLoader:
    """Load one resource of one approved type through the READ_RESOURCE contract only."""

    def __init__(
        self,
        *,
        tool: AwsResourceTool,
        customer_id: str,
        aws_account_id: str,
        resource_type: str,
    ) -> None:
        if not isinstance(tool, AwsResourceTool):
            raise TypeError("tool must implement AwsResourceTool")
        for field_name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        scope = _ACTUAL_RESOURCE_SCOPES.get(resource_type)
        if scope is None:
            raise ActualEvidenceError(
                f"resource type {resource_type!r} has no Actual evidence scope"
            )
        self._tool = tool
        self._customer_id = customer_id
        self._aws_account_id = aws_account_id
        self._resource_type = resource_type
        self._scope = scope

    @property
    def resource_type(self) -> str:
        return self._resource_type

    def load(self, resource_id: str) -> ActualEvidence:
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("resource_id must be a non-empty string")
        query = AwsResourceQuery(
            customer_id=self._customer_id,
            aws_account_id=self._aws_account_id,
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type=self._resource_type,
            resource_id=resource_id,
        )
        view = self._tool.read_resource(query)
        self._validate_view(view, resource_id)
        return ActualEvidence(
            resource_type=self._resource_type,
            resource_id=resource_id,
            resource_document={
                "resource_type": view.resource_type,
                "resource_id": view.resource_id,
                "attributes": view.to_dict()["attributes"],
            },
            evidence_references=(actual_evidence_reference(self._resource_type, resource_id),),
        )

    def _validate_view(self, view: object, resource_id: str) -> None:
        if not isinstance(view, AwsResourceView):
            raise ActualEvidenceError("AWS Resource Tool returned an invalid view")
        if (
            view.aws_account_id != self._aws_account_id
            or view.resource_type != self._resource_type
            or view.resource_id != resource_id
        ):
            raise ActualEvidenceError(
                f"AWS Resource Tool returned a view outside the {self._scope.label} query scope"
            )
