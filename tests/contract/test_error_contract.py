"""Contract tests for public Backend API errors."""

import unittest

from packages.contracts import ApiError, ApiErrorResponse


class ApiErrorContractTest(unittest.TestCase):
    def test_error_response_uses_minimum_public_envelope(self) -> None:
        response = ApiErrorResponse(
            error=ApiError(code="ASSESSMENT_NOT_FOUND", message="Assessment not found")
        )

        self.assertEqual(
            response.to_dict(),
            {
                "error": {
                    "code": "ASSESSMENT_NOT_FOUND",
                    "message": "Assessment not found",
                }
            },
        )

    def test_empty_effective_rule_set_uses_the_stable_public_error_code(self) -> None:
        response = ApiErrorResponse(
            error=ApiError(
                code="EFFECTIVE_RULE_SET_EMPTY",
                message="No executable rules are available for the selected policy profile and phase.",
            )
        )

        self.assertEqual(
            response.to_dict(),
            {
                "error": {
                    "code": "EFFECTIVE_RULE_SET_EMPTY",
                    "message": (
                        "No executable rules are available for the selected policy profile and phase."
                    ),
                }
            },
        )

    def test_error_fields_must_be_non_empty_strings(self) -> None:
        with self.assertRaisesRegex(ValueError, "code must be a non-empty string"):
            ApiError(code=" ", message="Assessment not found")

        with self.assertRaisesRegex(TypeError, "message must be a string"):
            ApiError(code="ASSESSMENT_NOT_FOUND", message=500)

    def test_error_envelope_requires_public_error_detail(self) -> None:
        with self.assertRaisesRegex(TypeError, "error must be an ApiError"):
            ApiErrorResponse(error={"code": "INTERNAL_ERROR", "message": "Failed"})


if __name__ == "__main__":
    unittest.main()
