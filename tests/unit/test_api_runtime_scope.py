"""Tests for fail-closed Lambda deployment scope configuration."""

import os
import unittest
from unittest.mock import patch

from apps.backend.api.runtime import EnvironmentAssessmentScope
from apps.backend.auth import Principal, Role
from apps.backend.jobs import AssessmentScopeDenied

PRINCIPAL = Principal(
    subject="user-001",
    client_id="client-001",
    customer_id="cust-001",
    roles=frozenset({Role.USER}),
)


class EnvironmentAssessmentScopeTest(unittest.TestCase):
    def test_permits_only_the_configured_customer_selector_pair(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ASSESSMENT_SCOPE_JSON": (
                    '{"cust-001":[{"repository_id":"repo-001",'
                    '"policy_profile_id":"profile-mvp-baseline"}]}'
                )
            },
            clear=True,
        ):
            scope = EnvironmentAssessmentScope.from_environment()

        self.assertIsNone(
            scope.authorize(
                PRINCIPAL, repository_id="repo-001", policy_profile_id="profile-mvp-baseline"
            )
        )
        with self.assertRaises(AssessmentScopeDenied):
            scope.authorize(
                PRINCIPAL, repository_id="repo-002", policy_profile_id="profile-mvp-baseline"
            )

    def test_missing_configuration_denies_every_selector(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            scope = EnvironmentAssessmentScope.from_environment()

        with self.assertRaises(AssessmentScopeDenied):
            scope.authorize(PRINCIPAL, repository_id="repo-001", policy_profile_id="profile-001")
