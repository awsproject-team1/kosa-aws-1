"""Resource ownership authorization for persisted Jobs."""

from apps.backend.auth import AuthorizationDenied, Principal, Role
from apps.backend.jobs.models import Job


def authorize_job_read(principal: Principal, job: Job) -> None:
    """Allow Admin access or require a User to own the requested Job."""
    if not isinstance(principal, Principal):
        raise TypeError("principal must be a Principal")
    if not isinstance(job, Job):
        raise TypeError("job must be a Job")

    if Role.ADMIN in principal.roles:
        return
    if Role.USER in principal.roles and principal.subject == job.requested_by:
        return
    raise AuthorizationDenied("principal is not authorized to read this job")
