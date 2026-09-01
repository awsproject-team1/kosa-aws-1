"""Small API Gateway adapter that keeps AWS event shapes outside application services."""

from __future__ import annotations

import json
from collections.abc import Mapping

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.deployments import DeploymentApiService, DeploymentApprovalRequest
from apps.backend.api.jobs import (
    AssessmentRequest,
    JobApiService,
)
from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import InvalidIdentityClaims, Principal
from apps.backend.jobs import JobNotFoundError, RequestValidationError, sanitize_public_failure
from packages.contracts import ApiErrorResponse


class JobHttpHandler:
    """Translate the two M0 Job routes to a typed, injected application service."""

    def __init__(
        self,
        service: JobApiService,
        assessment_reports: AssessmentReportApiService | None = None,
        remediations: RemediationApiService | None = None,
        deployments: DeploymentApiService | None = None,
    ) -> None:
        if not isinstance(service, JobApiService):
            raise TypeError("service must be a JobApiService")
        if assessment_reports is not None and not isinstance(
            assessment_reports, AssessmentReportApiService
        ):
            raise TypeError("assessment_reports must be an AssessmentReportApiService or None")
        self._service = service
        self._assessment_reports = assessment_reports
        if remediations is not None and not isinstance(remediations, RemediationApiService):
            raise TypeError("remediations must be a RemediationApiService or None")
        self._remediations = remediations
        if deployments is not None and not isinstance(deployments, DeploymentApiService):
            raise TypeError("deployments must be a DeploymentApiService or None")
        self._deployments = deployments

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
            if (
                method == "POST"
                and path.startswith("/findings/")
                and path.endswith("/remediations")
            ):
                finding_id = path.removeprefix("/findings/").removesuffix("/remediations")
                if not finding_id or "/" in finding_id or self._remediations is None:
                    raise JobNotFoundError("finding not found")
                if event.get("body") not in (None, "", "{}"):
                    raise RequestValidationError("remediation body is invalid")
                return _response(
                    202, self._remediations.create_remediation(principal, finding_id).to_dict()
                )
            if method == "POST" and path.startswith("/deployments/") and path.endswith("/approve"):
                deployment_id = path.removeprefix("/deployments/").removesuffix("/approve")
                if not deployment_id or "/" in deployment_id or self._deployments is None:
                    raise JobNotFoundError("deployment not found")
                try:
                    request = _deployment_approval_request(event.get("body"))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("approval body is invalid") from error
                return _response(
                    200, self._deployments.approve(principal, deployment_id, request).to_dict()
                )
            if method == "GET" and path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/")
                if not job_id or "/" in job_id:
                    raise JobNotFoundError("job not found")
                return _response(200, self._service.get_job(principal, job_id).to_dict())
            if method == "GET" and path.startswith("/assessments/"):
                assessment_id = path.removeprefix("/assessments/")
                if not assessment_id or "/" in assessment_id or self._assessment_reports is None:
                    raise JobNotFoundError("assessment not found")
                limit, cursor = _report_page_request(event.get("queryStringParameters"))
                try:
                    report = self._assessment_reports.get_assessment(
                        principal, assessment_id, limit=limit, cursor=cursor
                    )
                except ValueError as error:
                    raise RequestValidationError("assessment report query is invalid") from error
                return _response(200, report.to_dict())
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


def _deployment_approval_request(raw_body: object) -> DeploymentApprovalRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    if set(body) != {"commit_sha", "plan_hash"}:
        raise ValueError("approval body fields are invalid")
    return DeploymentApprovalRequest(commit_sha=body["commit_sha"], plan_hash=body["plan_hash"])


def _report_page_request(raw_query: object) -> tuple[int, str | None]:
    if raw_query is None:
        return 50, None
    query = _mapping(raw_query)
    if set(query) - {"limit", "cursor"}:
        raise RequestValidationError("assessment report query is invalid")
    limit_raw = query.get("limit", "50")
    if not isinstance(limit_raw, str) or not limit_raw.isdigit():
        raise RequestValidationError("assessment report limit is invalid")
    limit = int(limit_raw)
    if not 1 <= limit <= 100:
        raise RequestValidationError("assessment report limit is invalid")
    cursor = query.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise RequestValidationError("assessment report cursor is invalid")
    return limit, cursor


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
