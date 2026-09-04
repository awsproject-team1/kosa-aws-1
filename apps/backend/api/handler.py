"""Small API Gateway adapter that keeps AWS event shapes outside application services."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from apps.backend.api.assessments import AssessmentReportApiService
from apps.backend.api.audit_events import AuditEventApiService
from apps.backend.api.deployments import (
    DeploymentApiService,
    DeploymentApprovalRequest,
    DeploymentRejectRequest,
)
from apps.backend.api.jobs import (
    AssessmentRequest,
    JobApiService,
)
from apps.backend.api.observability import DemoRunObservabilityService
from apps.backend.api.policy_approval import PolicyApprovalApiService
from apps.backend.api.policy_candidates import PolicyCandidateApiService
from apps.backend.api.policy_sources import PolicySourceApiService
from apps.backend.api.remediation_exceptions import (
    RemediationExceptionApiService,
    RemediationExceptionRequest,
)
from apps.backend.api.remediations import RemediationApiService
from apps.backend.auth import AuthorizationDenied, InvalidIdentityClaims, Principal
from apps.backend.jobs import (
    JobNotFoundError,
    OrchestrationUnavailableError,
    RequestValidationError,
    sanitize_public_failure,
)
from packages.contracts import (
    ApiErrorResponse,
    DeploymentRejectionReason,
    OrchestrationRequest,
    PolicyRuleReference,
    PolicySourceUploadRequest,
    RemediationExceptionReason,
)


class JobHttpHandler:
    """Translate the two M0 Job routes to a typed, injected application service."""

    def __init__(
        self,
        service: JobApiService,
        assessment_reports: AssessmentReportApiService | None = None,
        remediations: RemediationApiService | None = None,
        remediation_exceptions: RemediationExceptionApiService | None = None,
        deployments: DeploymentApiService | None = None,
        policy_sources: PolicySourceApiService | None = None,
        policy_approvals: PolicyApprovalApiService | None = None,
        policy_candidates: PolicyCandidateApiService | None = None,
        audit_events: AuditEventApiService | None = None,
        observability: DemoRunObservabilityService | None = None,
        policy_reader: object | None = None,
        orchestrations: object | None = None,
        users: object | None = None,
        scope: object | None = None,
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
        if remediation_exceptions is not None and not isinstance(
            remediation_exceptions, RemediationExceptionApiService
        ):
            raise TypeError(
                "remediation_exceptions must be a RemediationExceptionApiService or None"
            )
        self._remediation_exceptions = remediation_exceptions
        if deployments is not None and not isinstance(deployments, DeploymentApiService):
            raise TypeError("deployments must be a DeploymentApiService or None")
        self._deployments = deployments
        if policy_sources is not None and not isinstance(policy_sources, PolicySourceApiService):
            raise TypeError("policy_sources must be a PolicySourceApiService or None")
        if policy_approvals is not None and not isinstance(
            policy_approvals, PolicyApprovalApiService
        ):
            raise TypeError("policy_approvals must be a PolicyApprovalApiService or None")
        if policy_candidates is not None and not isinstance(
            policy_candidates, PolicyCandidateApiService
        ):
            raise TypeError("policy_candidates must be a PolicyCandidateApiService or None")
        if audit_events is not None and not isinstance(audit_events, AuditEventApiService):
            raise TypeError("audit_events must be an AuditEventApiService or None")
        if observability is not None and not isinstance(observability, DemoRunObservabilityService):
            raise TypeError("observability must be a DemoRunObservabilityService or None")
        self._observability = observability
        self._policy_sources = policy_sources
        self._policy_approvals = policy_approvals
        self._policy_candidates = policy_candidates
        self._audit_events = audit_events
        self._policy_reader = policy_reader
        # Duck-typed to avoid importing the LangGraph-backed service (and its Layer-only
        # dependency) into the handler module; only .orchestrate(principal, request) is used.
        self._orchestrations = orchestrations
        # Duck-typed user-management service: create_user/list_users/assign_profile.
        self._users = users
        # Duck-typed scope read service: get_scope(principal).
        self._scope = scope

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
            if method == "POST" and path == "/orchestrate":
                if self._orchestrations is None:
                    raise JobNotFoundError("orchestrate route not found")
                try:
                    orchestration_request = _orchestration_request(event.get("body"))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("orchestrate body is invalid") from error
                try:
                    decision = self._orchestrations.orchestrate(principal, orchestration_request)
                except (AuthorizationDenied, InvalidIdentityClaims):
                    # Authorization is a real 401/403 the caller must see, not an assistant fault.
                    raise
                except Exception as error:
                    # The message parsed, so this is not a 400: the model call failed or returned a
                    # shape the router rejected (OrchestrationError is a ValueError that would
                    # otherwise fall through to an opaque 500). Surface it as a retryable 502; the
                    # 5xx logger still records the concrete exception for diagnosis.
                    raise OrchestrationUnavailableError(
                        "parent orchestrator could not produce a decision"
                    ) from error
                return _response(200, decision.to_dict())
            if method == "POST" and path == "/policy-sources/uploads":
                if self._policy_sources is None:
                    raise JobNotFoundError("policy source route not found")
                try:
                    request = _policy_upload_request(event.get("body"))
                    response = self._policy_sources.create_upload_session(principal, request)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("policy source upload body is invalid") from error
                return _response(201, response.to_dict())
            if method == "GET" and path == "/policy-sources":
                if self._policy_sources is None:
                    raise JobNotFoundError("policy source route not found")
                sources = self._policy_sources.list_sources(principal)
                return _response(200, {"sources": list(sources)})
            if method == "GET" and path == "/scope":
                if self._scope is None:
                    raise JobNotFoundError("scope route not found")
                return _response(200, self._scope.get_scope(principal))
            if method == "GET" and path == "/admin/users":
                if self._users is None:
                    raise JobNotFoundError("user management route not found")
                return _response(200, {"users": list(self._users.list_users(principal))})
            if method == "POST" and path == "/admin/users":
                if self._users is None:
                    raise JobNotFoundError("user management route not found")
                try:
                    body = _mapping(
                        json.loads(
                            event.get("body") if isinstance(event.get("body"), str) else "{}"
                        )
                    )
                    from apps.backend.api.users import CreateUserRequest

                    req = CreateUserRequest(
                        email=_non_empty_string(body.get("email"), "email"),
                        role=_non_empty_string(body.get("role"), "role"),
                        temporary_password=_non_empty_string(
                            body.get("temporary_password"), "temporary_password"
                        ),
                    )
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("user create body is invalid") from error
                return _response(201, self._users.create_user(principal, req))
            if method == "POST" and path == "/admin/users/profile":
                if self._users is None:
                    raise JobNotFoundError("user management route not found")
                try:
                    body = _mapping(
                        json.loads(
                            event.get("body") if isinstance(event.get("body"), str) else "{}"
                        )
                    )
                    email = _non_empty_string(body.get("email"), "email")
                    pid = _non_empty_string(body.get("policy_profile_id"), "policy_profile_id")
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("profile assign body is invalid") from error
                return _response(
                    200, self._users.assign_profile(principal, email=email, policy_profile_id=pid)
                )
            if method == "DELETE" and path == "/admin/users":
                if self._users is None:
                    raise JobNotFoundError("user management route not found")
                try:
                    body = _mapping(
                        json.loads(
                            event.get("body") if isinstance(event.get("body"), str) else "{}"
                        )
                    )
                    email = _non_empty_string(body.get("email"), "email")
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("user delete body is invalid") from error
                return _response(200, self._users.delete_user(principal, email=email))
            policy_path = _policy_source_path(path)
            if policy_path is not None and self._policy_sources is not None:
                source_id, source_version, action = policy_path
                if method == "GET" and action is None:
                    return _response(
                        200,
                        self._policy_sources.get_status(
                            principal, source_id=source_id, source_version=source_version
                        ).to_dict(),
                    )
                if method == "DELETE" and action is None:
                    self._policy_sources.delete_source(
                        principal, source_id=source_id, source_version=source_version
                    )
                    return _response(
                        200,
                        {"deleted": True, "source_id": source_id, "source_version": source_version},
                    )
                if method == "POST" and action == "process":
                    if self._policy_reader is None:
                        raise JobNotFoundError("policy process route not found")
                    try:
                        response = self._policy_sources.process_upload(
                            principal,
                            source_id=source_id,
                            source_version=source_version,
                            reader=self._policy_reader,
                        )
                    except ValueError as error:
                        raise RequestValidationError(
                            "policy source process request is invalid"
                        ) from error
                    return _response(202, response.to_dict())
                if action == "candidates":
                    if self._policy_candidates is None:
                        raise JobNotFoundError("policy candidate route not found")
                    if method == "POST":
                        if event.get("body") not in (None, "", "{}"):
                            raise RequestValidationError("policy candidate body is invalid")
                        accepted = self._policy_candidates.request_extraction(
                            principal, source_id=source_id, source_version=source_version
                        )
                        return _response(202, accepted.to_dict())
                    if method == "GET":
                        limit, cursor = _candidate_page_query(event.get("queryStringParameters"))
                        try:
                            page = self._policy_candidates.list_candidates(
                                principal,
                                source_id=source_id,
                                source_version=source_version,
                                **({} if cursor is None else {"cursor": cursor}),
                                **({} if limit is None else {"limit": limit}),
                            )
                        except ValueError as error:
                            raise RequestValidationError(
                                "policy candidate query is invalid"
                            ) from error
                        return _response(200, page.to_dict())
                if method == "POST" and action == "approve" and self._policy_approvals is not None:
                    try:
                        approved_rules = _policy_approval_request(event.get("body"))
                        response = self._policy_approvals.approve(
                            principal,
                            source_id=source_id,
                            source_version=source_version,
                            approved_rules=approved_rules,
                        )
                    except (TypeError, ValueError, json.JSONDecodeError) as error:
                        raise RequestValidationError(
                            "policy approval request is invalid"
                        ) from error
                    return _response(200, response.to_dict())
            if (
                method == "POST"
                and path == "/policy-profiles"
                and self._policy_approvals is not None
            ):
                request = _policy_profile_request(event.get("body"))
                try:
                    response = self._policy_approvals.publish(principal, **request)
                except ValueError as error:
                    raise RequestValidationError("policy profile request is invalid") from error
                return _response(201, response.to_dict())
            if method == "POST" and path == "/remediation-exceptions":
                if self._remediation_exceptions is None:
                    raise JobNotFoundError("remediation exception route not found")
                try:
                    request = _remediation_exception_request(event.get("body"))
                    response = self._remediation_exceptions.create(principal, request)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("remediation exception body is invalid") from error
                return _response(201, response.to_dict())
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
                remediation = self._remediations.create_remediation(principal, finding_id)
                return _response(202 if remediation.accepted else 200, remediation.to_dict())
            if (
                method == "POST"
                and path.startswith("/remediations/")
                and path.endswith("/deployments")
            ):
                remediation_id = path.removeprefix("/remediations/").removesuffix("/deployments")
                if not remediation_id or "/" in remediation_id or self._deployments is None:
                    raise JobNotFoundError("remediation not found")
                if event.get("body") not in (None, "", "{}"):
                    raise RequestValidationError("deployment body is invalid")
                job = self._deployments.create_deployment(principal, remediation_id)
                return _response(202, job.to_response().to_dict())
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
            if method == "POST" and path.startswith("/deployments/") and path.endswith("/reject"):
                deployment_id = path.removeprefix("/deployments/").removesuffix("/reject")
                if not deployment_id or "/" in deployment_id or self._deployments is None:
                    raise JobNotFoundError("deployment not found")
                try:
                    reject_request = _deployment_reject_request(event.get("body"))
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RequestValidationError("reject body is invalid") from error
                return _response(
                    200,
                    self._deployments.reject(principal, deployment_id, reject_request).to_dict(),
                )
            if (
                method == "GET"
                and path.startswith("/deployments/")
                and path.endswith("/verification")
            ):
                deployment_id = path.removeprefix("/deployments/").removesuffix("/verification")
                if not deployment_id or "/" in deployment_id or self._deployments is None:
                    raise JobNotFoundError("deployment not found")
                return _response(
                    200, self._deployments.get_verification(principal, deployment_id).to_dict()
                )
            if (
                method == "GET"
                and path.startswith("/deployments/")
                and path.endswith("/observability")
            ):
                deployment_id = path.removeprefix("/deployments/").removesuffix("/observability")
                if not deployment_id or "/" in deployment_id or self._observability is None:
                    raise JobNotFoundError("deployment observability not found")
                record = self._observability.assemble(principal, deployment_id=deployment_id)
                return _response(200, record.to_dict())
            if method == "GET" and path.startswith("/deployments/"):
                deployment_id = path.removeprefix("/deployments/")
                if not deployment_id or "/" in deployment_id or self._deployments is None:
                    raise JobNotFoundError("deployment not found")
                return _response(
                    200, self._deployments.get_deployment(principal, deployment_id).to_dict()
                )
            if method == "GET" and path == "/audit-events":
                if self._audit_events is None:
                    raise JobNotFoundError("audit events route not found")
                limit, cursor, event_type = _audit_event_query(event.get("queryStringParameters"))
                page = self._audit_events.list_events(
                    principal, limit=limit, cursor=cursor, event_type=event_type
                )
                return _response(200, page.to_dict())
            if method == "GET" and path.startswith("/jobs/"):
                job_id = path.removeprefix("/jobs/")
                if not job_id or "/" in job_id:
                    raise JobNotFoundError("job not found")
                return _response(200, self._service.get_job(principal, job_id).to_dict())
            if method == "GET" and path.startswith("/assessments/"):
                assessment_id = path.removeprefix("/assessments/")
                if not assessment_id or "/" in assessment_id or self._assessment_reports is None:
                    raise JobNotFoundError("assessment not found")
                limit, cursor, findings_cursor = _report_page_request(
                    event.get("queryStringParameters")
                )
                try:
                    report = self._assessment_reports.get_assessment(
                        principal,
                        assessment_id,
                        limit=limit,
                        cursor=cursor,
                        findings_cursor=findings_cursor,
                    )
                except ValueError as error:
                    raise RequestValidationError("assessment report query is invalid") from error
                return _response(200, report.to_dict())
            raise JobNotFoundError("route not found")
        except InvalidIdentityClaims as error:
            return _public_error(error)
        except Exception as error:
            return _public_error(error)


def _audit_event_query(
    parameters: object,
) -> tuple[object | None, object | None, object | None]:
    """Read the audit page query without interpreting it.

    Validation belongs to `AuditEventApiService`, which owns the bounds and the known
    event-type vocabulary. Parsing here would put the same rules in two places.
    """
    if parameters is None:
        return None, None, None
    if not isinstance(parameters, Mapping):
        raise RequestValidationError("audit event query is invalid")
    return parameters.get("limit"), parameters.get("cursor"), parameters.get("event_type")


def _candidate_page_query(parameters: object) -> tuple[int | None, str | None]:
    """Read the candidate page query. Bounds belong to `PolicyCandidateApiService`.

    `limit`만 여기서 정수로 바꾼다 — query string은 항상 문자열이고, 서비스는 정수 계약을 갖는다.
    상한과 하한 판정은 서비스가 한다. 두 곳에 두면 하나만 바뀐다.
    """
    if parameters is None:
        return None, None
    if not isinstance(parameters, Mapping):
        raise RequestValidationError("policy candidate query is invalid")
    raw_limit = parameters.get("limit")
    if raw_limit is None:
        limit = None
    else:
        try:
            limit = int(str(raw_limit))
        except ValueError as error:
            raise RequestValidationError("policy candidate limit is invalid") from error
    cursor = parameters.get("cursor")
    if cursor is not None and (not isinstance(cursor, str) or not cursor.strip()):
        raise RequestValidationError("policy candidate cursor is invalid")
    return limit, cursor


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
        claims = _mapping(jwt.get("claims"))
    except (TypeError, ValueError) as error:
        raise InvalidIdentityClaims("verified JWT claims are required") from error
    return _normalize_authorizer_claims(claims)


def _normalize_authorizer_claims(claims: Mapping[str, object]) -> Mapping[str, object]:
    """Restore array-shaped claims the HTTP API JWT authorizer serialized as text.

    The identity boundary requires `cognito:groups` as an array of strings, but the
    HTTP API JWT authorizer flattens multi-valued claims into a single bracketed,
    space-separated string such as `[Admin]` or `[Admin User]`. This adapter step
    restores that one claim to the array shape the boundary already validates,
    without inventing membership: a non-bracketed or empty value yields an empty
    list, which the boundary then rejects as it would any missing role.
    """
    raw_groups = claims.get("cognito:groups")
    if not isinstance(raw_groups, str):
        return claims
    text = raw_groups.strip()
    if text.startswith("[") and text.endswith("]"):
        groups = [group for group in text[1:-1].split() if group]
    else:
        groups = [group for group in text.split() if group]
    return {**claims, "cognito:groups": groups}


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


def _orchestration_request(raw_body: object) -> OrchestrationRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    allowed = {"message", "policy_profile_id"}
    if set(body) - allowed or "message" not in body:
        raise ValueError("orchestrate body fields are invalid")
    profile_id = body.get("policy_profile_id")
    return OrchestrationRequest(
        message=_non_empty_string(body["message"], "message"),
        policy_profile_id=(
            _non_empty_string(profile_id, "policy_profile_id") if profile_id is not None else None
        ),
    )


def _remediation_exception_request(raw_body: object) -> RemediationExceptionRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    required = {"rule_id", "rule_version", "reason", "expires_at"}
    allowed = required | {"resource_id", "ticket_reference"}
    if set(body) - allowed or not required.issubset(body):
        raise ValueError("remediation exception body fields are invalid")
    return RemediationExceptionRequest(
        rule_id=_non_empty_string(body["rule_id"], "rule_id"),
        rule_version=_non_empty_string(body["rule_version"], "rule_version"),
        reason=RemediationExceptionReason(body["reason"]),
        expires_at=_non_empty_string(body["expires_at"], "expires_at"),
        resource_id=body.get("resource_id"),
        ticket_reference=body.get("ticket_reference"),
    )


def _deployment_approval_request(raw_body: object) -> DeploymentApprovalRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    if set(body) != {"commit_sha", "plan_hash"}:
        raise ValueError("approval body fields are invalid")
    return DeploymentApprovalRequest(commit_sha=body["commit_sha"], plan_hash=body["plan_hash"])


def _deployment_reject_request(raw_body: object) -> DeploymentRejectRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    allowed = {"reason", "ticket_reference"}
    if set(body) - allowed or "reason" not in body:
        raise ValueError("reject body fields are invalid")
    return DeploymentRejectRequest(
        reason=DeploymentRejectionReason(body["reason"]),
        ticket_reference=body.get("ticket_reference"),
    )


def _policy_upload_request(raw_body: object) -> PolicySourceUploadRequest:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    if set(body) - {"filename", "declared_media_type", "byte_size", "title"} or not {
        "filename",
        "declared_media_type",
        "byte_size",
    }.issubset(body):
        raise ValueError("policy upload body fields are invalid")
    return PolicySourceUploadRequest(
        filename=body["filename"],
        declared_media_type=body["declared_media_type"],
        byte_size=body["byte_size"],
        title=body.get("title"),
    )


def _policy_source_path(path: str) -> tuple[str, str, str | None] | None:
    parts = path.split("/")
    if len(parts) not in {5, 6} or parts[:2] != ["", "policy-sources"] or parts[3] != "versions":
        return None
    source_id, source_version = parts[2], parts[4]
    if (
        not source_id
        or not source_version
        or (len(parts) == 6 and parts[5] not in {"process", "approve", "candidates"})
    ):
        return None
    return source_id, source_version, parts[5] if len(parts) == 6 else None


def _policy_approval_request(raw_body: object) -> tuple[PolicyRuleReference, ...]:
    """승인할 Rule 목록을 파싱한다. body는 `{"approved_rules": [{rule_id, version}, ...]}`."""
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    if set(body) != {"approved_rules"}:
        raise ValueError("policy approval body fields are invalid")
    entries = body["approved_rules"]
    if not isinstance(entries, list) or not entries:
        raise ValueError("approved_rules must be a non-empty list")
    references: list[PolicyRuleReference] = []
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != {"rule_id", "version"}:
            raise ValueError("approved_rules items must be {rule_id, version}")
        references.append(
            PolicyRuleReference(
                rule_id=_non_empty_string(entry["rule_id"], "rule_id"),
                version=_non_empty_string(entry["version"], "version"),
            )
        )
    return tuple(references)


def _policy_profile_request(raw_body: object) -> dict[str, str]:
    if not isinstance(raw_body, str):
        raise ValueError("body must be a JSON string")
    body = _mapping(json.loads(raw_body))
    expected = {"source_id", "source_version", "policy_profile_id", "version"}
    if set(body) != expected or not all(
        isinstance(body[name], str) and body[name] for name in expected
    ):
        raise ValueError("policy profile body fields are invalid")
    return {name: body[name] for name in expected}


def _report_page_request(raw_query: object) -> tuple[int, str | None, str | None]:
    if raw_query is None:
        return 50, None, None
    query = _mapping(raw_query)
    if set(query) - {"limit", "cursor", "findings_cursor"}:
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
    findings_cursor = query.get("findings_cursor")
    if findings_cursor is not None and (
        not isinstance(findings_cursor, str) or not findings_cursor
    ):
        raise RequestValidationError("assessment report findings cursor is invalid")
    return limit, cursor, findings_cursor


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
    # Server faults (5xx) are unmapped and would otherwise vanish: the public body carries only a
    # code. Log the exception type and message (never request bodies or policy text) so a 500 is
    # diagnosable. 4xx are expected client outcomes and are not logged as errors.
    if failure.status_code >= 500:
        logging.getLogger("governance.api").exception(
            "unhandled API failure: %s: %s", type(error).__name__, error
        )
    return _response(failure.status_code, ApiErrorResponse(error=failure.error).to_dict())
