"""Small API Gateway adapter that keeps AWS event shapes outside application services."""

from __future__ import annotations

import json
from collections.abc import Mapping

from apps.backend.api.jobs import (
    AssessmentRequest,
    JobApiService,
)
from apps.backend.auth import InvalidIdentityClaims, Principal
from apps.backend.jobs import JobNotFoundError, RequestValidationError, sanitize_public_failure
from packages.contracts import ApiErrorResponse


class JobHttpHandler:
    """Translate the two M0 Job routes to a typed, injected application service."""

    def __init__(self, service: JobApiService) -> None:
        if not isinstance(service, JobApiService):
            raise TypeError("service must be a JobApiService")
        self._service = service

    def handle(self, event: Mapping[str, object]) -> dict[str, object]:
        """Return an API Gateway proxy response without leaking exception details."""
        try:
            method, path, claims = _request_parts(event)
            principal = Principal.from_verified_claims(claims)
            if method == "POST" and path == "/assessments":
                try:
                    request = _assessment_request(event.get("body"))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("assessment body is invalid") from error
                response = self._service.create_assessment(principal, request)
                return _response(202, response.to_dict())
            if method == "GET" and path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/")
                if not job_id or "/" in job_id:
                    raise JobNotFoundError("job not found")
                return _response(200, self._service.get_job(principal, job_id).to_dict())
            raise JobNotFoundError("route not found")
        except InvalidIdentityClaims as error:
            return _public_error(error)
        except Exception as error:
            return _public_error(error)


def _request_parts(event: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    request_context = _mapping(event.get("requestContext"))
    http = _mapping(request_context.get("http"))
    claims = _identity_claims(request_context)
    method = _non_empty_string(http.get("method"), "method")
    path = _non_empty_string(event.get("rawPath"), "rawPath")
    return method, path, claims


def _identity_claims(request_context: Mapping[str, object]) -> Mapping[str, object]:
    try:
        authorizer = _mapping(request_context.get("authorizer"))
        jwt = _mapping(authorizer.get("jwt"))
        return _mapping(jwt.get("claims"))
    except (TypeError, ValueError) as error:
        raise InvalidIdentityClaims("verified JWT claims are required") from error


def _assessment_request(raw_body: object) -> AssessmentRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    expected = {"repository_id", "policy_profile_id"}
    if set(body) != expected:
        raise ValueError("assessment body fields are invalid")
    return AssessmentRequest(
        repository_id=_non_empty_string(body["repository_id"], "repository_id"),
        policy_profile_id=_non_empty_string(body["policy_profile_id"], "policy_profile_id"),
    )


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("expected a mapping")
    return value


def _non_empty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _response(status_code: int, body: dict[str, object]) -> dict[str, object]:
    return {
        "statusCode": status_code,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _public_error(error: BaseException) -> dict[str, object]:
    failure = sanitize_public_failure(error)
    return _response(failure.status_code, ApiErrorResponse(error=failure.error).to_dict())
