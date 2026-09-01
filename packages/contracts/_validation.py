"""Small runtime checks shared by transport contract values."""

from datetime import datetime


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


def require_offset_aware_timestamp(value: object, field_name: str) -> datetime:
    """Require an ISO-8601 timestamp that carries an explicit UTC offset.

    승인 시각처럼 표시만 하는 값과 달리, 비교되는 시각은 offset이 없으면 순서를 정할 수 없다.
    naive 문자열을 받아 로컬 시간으로 해석하면 만료 판정이 실행 환경에 따라 달라진다.
    """
    require_non_empty_string(value, field_name)
    assert isinstance(value, str)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must carry an explicit UTC offset")
    return parsed
