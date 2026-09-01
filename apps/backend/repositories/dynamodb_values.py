"""Provider-neutral DynamoDB low-level AttributeValue serialization."""

from collections.abc import Mapping
from decimal import Decimal


def marshal_item(item: Mapping[str, object]) -> dict[str, object]:
    """Convert application values to the DynamoDB client AttributeValue shape."""
    return {key: marshal_value(value) for key, value in item.items()}


def marshal_value(value: object) -> dict[str, object]:
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return {"N": str(value)}
    if isinstance(value, float):
        return {"N": str(Decimal(str(value)))}
    if isinstance(value, Mapping):
        return {"M": marshal_item(value)}
    if isinstance(value, (list, tuple)):
        return {"L": [marshal_value(entry) for entry in value]}
    raise TypeError(f"unsupported DynamoDB transaction attribute type: {type(value).__name__}")
