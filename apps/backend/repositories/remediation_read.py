"""A-owned read of one `REMEDIATION#{id}` item for the public remediation view.

`DynamoDbDeploymentSourceReader`가 같은 item을 배포 생성 입력으로 읽는다. 이 reader는 그 item을
화면용 projection으로만 되돌린다 — GitHub를 읽지 않고, commit 도달 가능성도 판단하지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from apps.backend.repositories.dynamodb import DynamoTable
from apps.backend.repositories.ports import RepositoryError, StoredDataError


class RemediationNotFoundError(LookupError):
    """The remediation does not exist in the caller's customer partition."""


@dataclass(frozen=True, slots=True, kw_only=True)
class RemediationView:
    """Public read projection of one stored remediation record.

    api 계층이 아니라 여기 있는 이유는 import 방향이다: `apps.backend.repositories`가
    `apps.backend.api`를 import하면 `api → jobs → repositories.ports → repositories`로 순환한다.
    """

    remediation_id: str
    finding_id: str
    status: str
    decision: Mapping[str, object]
    job_id: str | None
    decided_at: str | None
    result: Mapping[str, object] | None
    pull_request: Mapping[str, object] | None
    #: 조치가 끝내 실패한 사유(`code`, `reason`, `failed_at`). 없으면 실패하지 않았다.
    failure: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        for name in ("remediation_id", "finding_id", "status"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not isinstance(self.decision, Mapping):
            raise TypeError("decision must be a mapping")
        for name in ("result", "pull_request", "failure"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping or None")

    def to_dict(self) -> dict[str, object]:
        return {
            "remediation_id": self.remediation_id,
            "finding_id": self.finding_id,
            "status": self.status,
            "decision": dict(self.decision),
            "job_id": self.job_id,
            "decided_at": self.decided_at,
            "result": None if self.result is None else dict(self.result),
            "pull_request": None if self.pull_request is None else dict(self.pull_request),
            "failure": None if self.failure is None else dict(self.failure),
        }


class DynamoDbRemediationReadRepository:
    """Read one remediation record inside one customer partition."""

    def __init__(self, table: DynamoTable) -> None:
        if table is None:
            raise TypeError("table is required")
        self._table = table

    def get_remediation(self, *, customer_id: str, remediation_id: str) -> RemediationView:
        for value, name in ((customer_id, "customer_id"), (remediation_id, "remediation_id")):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        try:
            item = self._table.get_item(
                Key={"PK": f"CUSTOMER#{customer_id}", "SK": f"REMEDIATION#{remediation_id}"},
                ConsistentRead=True,
            ).get("Item")
        except Exception:
            raise RepositoryError("remediation read failed") from None
        if item is None:
            raise RemediationNotFoundError("remediation not found")
        if not isinstance(item, Mapping):
            raise StoredDataError("stored remediation is invalid")
        if item.get("entity_type") != "REMEDIATION" or item.get("customer_id") != customer_id:
            raise StoredDataError("stored remediation is outside the customer scope")
        decision = item.get("decision")
        if not isinstance(decision, Mapping):
            raise StoredDataError("stored remediation decision is invalid")
        try:
            return RemediationView(
                remediation_id=_string(item.get("remediation_id")),
                finding_id=_string(item.get("finding_id")),
                status=_string(item.get("status")),
                decision=_plain(decision),
                job_id=_optional_string(item.get("job_id")),
                decided_at=_optional_string(item.get("decided_at")),
                result=_optional_mapping(item.get("result")),
                pull_request=_optional_mapping(item.get("pull_request")),
                failure=_optional_mapping(item.get("failure")),
            )
        except (TypeError, ValueError) as error:
            raise StoredDataError("stored remediation is invalid") from error


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("stored remediation field must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _optional_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise TypeError("stored remediation field must be a mapping")
    return _plain(value)


def _plain(value: object) -> object:
    """Convert DynamoDB resource values (Decimal, nested maps) into JSON-safe values."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
