"""Remediation boundary before GitHub PR and deployment workflows."""

from apps.backend.remediation.service import RemediationContractError, RemediationService

__all__ = ["RemediationContractError", "RemediationService"]
