"""Small runtime checks shared by transport contract values."""


def require_non_empty_string(value: object, field_name: str) -> None:
    """Require an opaque string value without imposing an identifier format."""
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


def require_optional_non_empty_string(value: object, field_name: str) -> None:
    """Validate an optional opaque string value when it is present."""
    if value is not None:
        require_non_empty_string(value, field_name)
