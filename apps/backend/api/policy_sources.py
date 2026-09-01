"""A-owned customer-scoped Policy Source upload-session boundary."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from apps.backend.auth import Action, Principal, authorize
from apps.backend.policy.ingestion import NormalizationOutcome, normalize_upload
from apps.backend.policy.ingestion.pipeline import UploadedPolicyOriginal
from packages.contracts import NormalizedPolicyDocument, PolicySourceUploadRequest


@dataclass(frozen=True, slots=True, kw_only=True)
class PolicySourceUploadSession:
    source_id: str
    source_version: str
    upload_url: str

    def __post_init__(self) -> None:
        for value in (self.source_id, self.source_version, self.upload_url):
            if not isinstance(value, str) or not value.strip():
                raise ValueError("upload session values must be non-empty strings")

    def to_dict(self) -> dict[str, str]:
        return {
            "source_id": self.source_id,
            "source_version": self.source_version,
            "upload_url": self.upload_url,
        }


class PolicySourceUploadRepository(Protocol):
    def create_upload_session(
        self,
        *,
        customer_id: str,
        request: PolicySourceUploadRequest,
        source_id: str,
        source_version: str,
    ) -> PolicySourceUploadSession: ...

    def finalize_upload(
        self,
        *,
        customer_id: str,
        source_id: str,
        source_version: str,
        reader: object,
    ) -> tuple[UploadedPolicyOriginal, bytes]: ...

    def record_normalization(
        self,
        *,
        customer_id: str,
        document: NormalizedPolicyDocument,
        normalized_payload: bytes | None,
    ) -> None: ...

    def get_document(
        self, *, customer_id: str, source_id: str, source_version: str
    ) -> NormalizedPolicyDocument: ...


class PolicySourceApiService:
    """Issue one backend-owned upload session; client cannot select tenant storage identity."""

    def __init__(
        self,
        *,
        repository: PolicySourceUploadRepository,
        source_id_factory: Callable[[], str],
        source_version_factory: Callable[[], str],
    ) -> None:
        if (
            repository is None
            or not callable(source_id_factory)
            or not callable(source_version_factory)
        ):
            raise TypeError("repository and ID factories are required")
        self._repository = repository
        self._source_id_factory = source_id_factory
        self._source_version_factory = source_version_factory

    def create_upload_session(
        self, principal: Principal, request: PolicySourceUploadRequest
    ) -> PolicySourceUploadSession:
        if not isinstance(principal, Principal) or not isinstance(
            request, PolicySourceUploadRequest
        ):
            raise TypeError("principal and request are required")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        source_id = self._new(self._source_id_factory, "source_id")
        source_version = self._new(self._source_version_factory, "source_version")
        return self._repository.create_upload_session(
            customer_id=principal.customer_id,
            request=request,
            source_id=source_id,
            source_version=source_version,
        )

    def process_upload(
        self,
        principal: Principal,
        *,
        source_id: str,
        source_version: str,
        reader: object,
    ) -> NormalizedPolicyDocument:
        """Finalize one server-owned original, normalize it, then persist metadata.

        This is the worker-facing operation.  It deliberately accepts no object
        key, checksum, parser status, or customer ID from the caller.
        """
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        _non_empty(source_id, "source_id")
        _non_empty(source_version, "source_version")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        original, payload = self._repository.finalize_upload(
            customer_id=principal.customer_id,
            source_id=source_id,
            source_version=source_version,
            reader=reader,
        )
        outcome: NormalizationOutcome = normalize_upload(original, payload)
        self._repository.record_normalization(
            customer_id=principal.customer_id,
            document=outcome.document,
            normalized_payload=outcome.normalized_payload,
        )
        return outcome.document

    def get_status(
        self, principal: Principal, *, source_id: str, source_version: str
    ) -> NormalizedPolicyDocument:
        if not isinstance(principal, Principal):
            raise TypeError("principal must be a Principal")
        _non_empty(source_id, "source_id")
        _non_empty(source_version, "source_version")
        authorize(principal, Action.MANAGE_POLICY_SOURCES)
        return self._repository.get_document(
            customer_id=principal.customer_id,
            source_id=source_id,
            source_version=source_version,
        )

    @staticmethod
    def _new(factory: Callable[[], str], name: str) -> str:
        value = factory()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"generated {name} must be a non-empty string")
        return value


def _non_empty(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
