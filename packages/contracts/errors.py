"""Public Backend API error transport contracts."""

from dataclasses import dataclass

from packages.contracts._validation import require_non_empty_string


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiError:
    """Stable public error detail without internal exception or provider data."""

    code: str
    message: str

    def __post_init__(self) -> None:
        require_non_empty_string(self.code, "code")
        require_non_empty_string(self.message, "message")

    def to_dict(self) -> dict[str, str]:
        """Return the public error detail wire shape."""
        return {"code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True, kw_only=True)
class ApiErrorResponse:
    """Top-level public API error envelope."""

    error: ApiError

    def __post_init__(self) -> None:
        if not isinstance(self.error, ApiError):
            raise TypeError("error must be an ApiError")

    def to_dict(self) -> dict[str, dict[str, str]]:
        """Return the public API error response wire shape."""
        return {"error": self.error.to_dict()}
