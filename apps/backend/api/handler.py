"""Small API Gateway adapter that keeps AWS event shapes outside application services."""

from __future__ import annotations

import json
from collections.abc import Mapping

from apps.backend.api.jobs import (
    AssessmentRequest,
    AssessmentScopeDenied,
    JobApiService,
    JobNotFoundError,
)
from apps.backend.auth import AuthorizationDenied, InvalidIdentityClaims, Principal
from apps.backend.repositories import DuplicateJobError, RepositoryError
from packages.contracts import ApiError, ApiErrorResponse


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
                request = _assessment_request(event.get("body"))
                response = self._service.create_assessment(principal, request)
                return _response(202, response.to_dict())
            if method == "GET" and path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/")
                if not job_id or "/" in job_id:
                    return _error(404, "NOT_FOUND", "The requested resource was not found")
                return _response(200, self._service.get_job(principal, job_id).to_dict())
            return _error(404, "NOT_FOUND", "The requested resource was not found")
        except InvalidIdentityClaims:
            return _error(401, "UNAUTHORIZED", "Authentication is required")
        except (AuthorizationDenied, AssessmentScopeDenied):
            return _error(
                403, "SCOPE_DENIED", "The requested resource is outside the approved scope"
            )
        except JobNotFoundError:
            return _error(404, "NOT_FOUND", "The requested resource was not found")
        except (TypeError, ValueError, json.JSONDecodeError):
            return _error(400, "VALIDATION_ERROR", "The request is invalid")
        except DuplicateJobError:
            return _error(409, "CONFLICT", "The request conflicts with current state")
        except RepositoryError:
            return _error(503, "EXECUTION_ERROR", "The service is temporarily unavailable")
        except Exception:
            return _error(500, "EXECUTION_ERROR", "An internal error occurred")


def _request_parts(event: Mapping[str, object]) -> tuple[str, str, Mapping[str, object]]:
    if not isinstance(event, Mapping):
        raise TypeError("event must be a mapping")
    request_context = _mapping(event.get("requestContext"))
    http = _mapping(request_context.get("http"))
    authorizer = _mapping(request_context.get("authorizer"))
    jwt = _mapping(authorizer.get("jwt"))
    claims = _mapping(jwt.get("claims"))
    method = _non_empty_string(http.get("method"), "method")
    path = _non_empty_string(event.get("rawPath"), "rawPath")
    return method, path, claims


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


def _error(status_code: int, code: str, message: str) -> dict[str, object]:
    return _response(
        status_code,
        ApiErrorResponse(error=ApiError(code=code, message=message)).to_dict(),
    )
