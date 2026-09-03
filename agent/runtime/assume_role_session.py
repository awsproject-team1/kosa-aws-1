"""Shared AssumeRole session for the read-only AWS Resource Tool adapters.

Every Actual-state adapter reaches the customer account the same way: one approved
Role ARN, a customer-bound ExternalId, and short-lived credentials that are refreshed
before they expire. Keeping that in one place means adding a resource type cannot
accidentally introduce a second, weaker way of obtaining credentials.

The session hands out credentials only; it never builds a client, so it cannot be the
place where a mutating API surface sneaks in.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from time import time

from agent.runtime.aws_resource_tool import AwsResourceToolError

#: Refresh this many seconds before expiry so an in-flight read cannot outlive its
#: credentials.
_REFRESH_MARGIN_SECONDS = 60

_REQUIRED_CREDENTIAL_FIELDS = ("AccessKeyId", "SecretAccessKey", "SessionToken")


class AssumeRoleReadSession:
    """Cache short-lived credentials for one approved read Role ARN."""

    def __init__(
        self,
        *,
        role_arn: str,
        external_id: str,
        sts: object,
        clock: Callable[[], float] = time,
        session_name: str = "governance-read",
    ) -> None:
        for name, value in (
            ("role_arn", role_arn),
            ("external_id", external_id),
            ("session_name", session_name),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if sts is None or not callable(clock):
            raise TypeError("sts and clock are required")
        self._role_arn = role_arn
        self._external_id = external_id
        self._sts = sts
        self._clock = clock
        self._session_name = session_name
        self._cached_credentials: Mapping[str, str] | None = None
        self._credentials_expire_at: float | None = None

    def credentials(self) -> Mapping[str, str]:
        """Return usable credentials, assuming the role again only when needed."""
        try:
            if self._cached_credentials is None or not self._credentials_are_valid():
                response = self._sts.assume_role(
                    RoleArn=self._role_arn,
                    RoleSessionName=self._session_name,
                    ExternalId=self._external_id,
                )
                values = response.get("Credentials")
                if not isinstance(values, Mapping):
                    raise ValueError
                required = {name: values[name] for name in _REQUIRED_CREDENTIAL_FIELDS}
                if not all(isinstance(value, str) and value for value in required.values()):
                    raise ValueError
                self._cached_credentials = required
                self._credentials_expire_at = expiration_epoch(values.get("Expiration"))
            return self._cached_credentials
        except Exception:
            # The failure reason can carry role/account detail, so it is not propagated.
            raise AwsResourceToolError("AWS read role assumption failed") from None

    def _credentials_are_valid(self) -> bool:
        return (
            self._credentials_expire_at is not None
            and self._credentials_expire_at > self._clock() + _REFRESH_MARGIN_SECONDS
        )


def expiration_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def error_code(error: Exception) -> str | None:
    """Return the AWS error code of a botocore-shaped client error, if present."""
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    value = details.get("Code") if isinstance(details, Mapping) else None
    return value if isinstance(value, str) else None


#: Continuation-token key names used by the read APIs behind these adapters. EC2 uses
#: `NextToken`, RDS and ELBv2 use `Marker`/`NextMarker`, and S3 `ListBuckets` uses
#: `ContinuationToken`.
_CONTINUATION_KEYS = ("NextToken", "NextMarker", "Marker", "ContinuationToken")

#: Hard cap on pages followed for one list. A list that needs more pages than this is
#: reported as a failure rather than truncated — a short list of resources is
#: indistinguishable from a compliant account, and post-deploy verification reads these
#: lists to decide whether a violation is gone (ADR-0020).
_MAX_LIST_PAGES = 200


def paginate(
    call: Callable[..., Mapping[str, object]],
    *,
    items_key: str,
    token_argument: str,
    request: Mapping[str, object] | None = None,
) -> list[Mapping[str, object]]:
    """Follow an AWS list API's continuation token and return every item.

    Returning only the first page would silently shrink an account: RDS answers 100 DB
    instances per page by default and ELBv2 answers 400, so a real customer account is
    routinely truncated. This helper is the single place that decides how far to follow.
    """
    items: list[Mapping[str, object]] = []
    parameters = dict(request or {})
    token: object = None
    for _ in range(_MAX_LIST_PAGES):
        if token is None:
            response = call(**parameters)
        else:
            response = call(**parameters, **{token_argument: token})
        if not isinstance(response, Mapping):
            raise AwsResourceToolError("AWS list response is invalid")
        page = response.get(items_key)
        if not isinstance(page, list):
            raise AwsResourceToolError("AWS list response is invalid")
        items.extend(item for item in page if isinstance(item, Mapping))
        token = _continuation(response)
        if token is None:
            return items
    raise AwsResourceToolError("AWS list exceeded the maximum number of pages")


def _continuation(response: Mapping[str, object]) -> str | None:
    for key in _CONTINUATION_KEYS:
        value = response.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def projected(source: object, fields: tuple[str, ...]) -> dict[str, object]:
    """Project an AWS response object down to an allow-list of descriptive fields.

    Read responses carry more than a compliance judgement needs (user data, endpoint
    credentials, tag values, master usernames). An allow-list keeps the evidence document
    to the fields the Rules actually cite, so a provider that starts returning a new field
    cannot widen what reaches the model or the stored evidence.
    """
    if not isinstance(source, Mapping):
        return {}
    return {field: _plain(source[field]) for field in fields if field in source}


def _plain(value: object) -> object:
    """Return a JSON-serializable copy, rendering datetimes as ISO-8601 strings."""
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (str, bytes, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)
