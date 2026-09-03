"""The one place that decides where a customer policy object lives in S3.

세 곳이 같은 key를 만든다: 업로드 세션(presigned PUT), 정규화 writer, 그리고 추출 worker의
Artifact Reader. 문자열을 각자 조립하면 셋 중 하나만 바뀌었을 때 **읽는 쪽이 조용히 다른 객체를
읽거나 아무것도 못 읽는다.** 후자는 그나마 실패로 드러나지만, 전자는 다른 고객·다른 판본의
문서를 추출 입력으로 삼는 경로다. key 조립을 한 함수로 모아 그 가능성을 없앤다.

key에 들어가는 값은 전부 서버가 발급한다. 클라이언트가 고른 문자열은 여기 오지 않는다.
"""

from __future__ import annotations

_KEY_FIELDS = ("customer_id", "source_id", "source_version")


def _require_key_component(value: object, field_name: str) -> str:
    """Reject anything that could escape its path segment.

    `/`나 `..`가 섞이면 한 고객의 key가 다른 고객의 prefix를 가리킬 수 있다. 이 값들은 모두
    서버가 발급하지만, 발급 규칙이 바뀌었을 때 조용히 통과하지 않도록 여기서 다시 막는다.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    if "/" in value or "\\" in value or value in {".", ".."}:
        raise ValueError(f"{field_name} must be a single path segment")
    return value


def _version_prefix(*, customer_id: str, source_id: str, source_version: str) -> str:
    values = {
        "customer_id": customer_id,
        "source_id": source_id,
        "source_version": source_version,
    }
    for field_name in _KEY_FIELDS:
        _require_key_component(values[field_name], field_name)
    return f"customers/{customer_id}/policy-sources/{source_id}/versions/{source_version}"


def original_object_key(*, customer_id: str, source_id: str, source_version: str) -> str:
    """The uploaded original bytes for one exact source version."""
    return f"{_version_prefix(customer_id=customer_id, source_id=source_id, source_version=source_version)}/original"


def normalized_object_key(*, customer_id: str, source_id: str, source_version: str) -> str:
    """The normalized artifact — the only object that carries policy text."""
    return f"{_version_prefix(customer_id=customer_id, source_id=source_id, source_version=source_version)}/normalized"


def normalized_artifact_id(original_artifact_id: str) -> str:
    """Derive the normalized artifact ID from the original it was produced from."""
    _require_key_component(original_artifact_id, "original_artifact_id")
    return f"{original_artifact_id}#normalized"
