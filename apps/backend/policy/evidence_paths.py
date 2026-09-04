"""Deterministic lookup of AWS Actual evidence inside a projected resource document.

`EvidenceCapabilityBinding.document_paths`가 가리키는 곳은 `ActualEvidenceLoader`가 만든
`resource_document`다. 그 문서는 adapter가 projection한 고정된 모양이므로, 필수 field가 실제로
채워졌는지를 모델을 부르기 전에 확인할 수 있다. 확인 없이 부르면 "근거가 없어서 판단 못 함"과
"모델이 판단을 회피함"이 같은 결과로 섞인다.

경로 문법은 세 가지 조각만 갖는다.

    attributes.public_access_block.BlockPublicAcls
    attributes.volumes[].Encrypted
    attributes.load_balancer_attributes.{access_logs.s3.enabled}

- `name` — mapping key.
- `name[]` — 비어 있지 않은 list여야 하고, 남은 경로가 **모든** 원소에서 풀려야 한다.
  하나라도 빠지면 그 리소스의 근거는 불완전하다.
- `{key}` — 점을 포함하는 mapping key(ALB attribute 이름). 점 구분자와 충돌하지 않게 감싼다.

`None`은 "없음"으로 본다. adapter는 응답이 보고하지 않은 field를 아예 넣지 않고, 값이 없는 것과
보고되지 않은 것을 구분하기 때문이다(`_selected_attributes`).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


class EvidencePathError(ValueError):
    """Raised when a declared evidence path is not well formed."""


@dataclass(frozen=True, slots=True)
class _Segment:
    key: str
    expands_list: bool


def parse_document_path(path: str) -> tuple[_Segment, ...]:
    """Parse one declared path, rejecting anything ambiguous at declaration time.

    Catalog를 만들 때 한 번 파싱해 두면, 오타 난 경로가 런타임에 "근거 없음"으로 조용히
    나타나는 대신 import 시점에 실패한다.
    """
    if not isinstance(path, str) or not path.strip():
        raise EvidencePathError("document path must be a non-empty string")
    segments: list[_Segment] = []
    index = 0
    length = len(path)
    while index < length:
        if path[index] == "{":
            close = path.find("}", index)
            if close == -1:
                raise EvidencePathError(f"unterminated braced key in {path!r}")
            key = path[index + 1 : close]
            index = close + 1
        else:
            end = index
            while end < length and path[end] not in ".[{":
                end += 1
            key = path[index:end]
            index = end
        if not key:
            raise EvidencePathError(f"empty path segment in {path!r}")
        expands_list = path.startswith("[]", index)
        if expands_list:
            index += 2
        segments.append(_Segment(key=key, expands_list=expands_list))
        if index < length:
            if path[index] != ".":
                raise EvidencePathError(f"unexpected character at {index} in {path!r}")
            index += 1
            if index == length:
                raise EvidencePathError(f"trailing separator in {path!r}")
    if not segments:
        raise EvidencePathError(f"document path {path!r} has no segments")
    return tuple(segments)


def document_path_present(document: object, path: str) -> bool:
    """Whether a projected resource document actually carries the evidence at `path`."""
    return _present(document, parse_document_path(path), 0)


def missing_document_paths(document: object, paths: Sequence[str]) -> tuple[str, ...]:
    """Return the declared paths this document does not carry, in declaration order."""
    return tuple(path for path in paths if not document_path_present(document, path))


def _present(value: object, segments: tuple[_Segment, ...], index: int) -> bool:
    if index == len(segments):
        return value is not None
    segment = segments[index]
    if not isinstance(value, Mapping):
        return False
    if segment.key not in value:
        return False
    child = value[segment.key]
    if child is None:
        return False
    if not segment.expands_list:
        return _present(child, segments, index + 1)
    if isinstance(child, (str, bytes)) or not isinstance(child, Sequence):
        return False
    if not child:
        # 빈 list는 "이 리소스에 해당 항목이 하나도 없다"이며, 근거가 수집된 상태가 아니다.
        return False
    return all(_present(entry, segments, index + 1) for entry in child)
