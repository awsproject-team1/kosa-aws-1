"""Bound a model-proposed Terraform change to the snapshot it claims to fix.

Remediation patch는 "이 commit의 이 파일들을 이렇게 바꾼다"는 제안이다. 모델이 원본을 보지 않고
파일 전체를 새로 쓰면 그 제안은 (1) 존재하지 않는 파일을 만들거나, (2) Finding과 무관한 설정을
지우거나, (3) 리소스 블록을 통째로 빼먹을 수 있고, 그중 무엇이 일어났는지 diff 없이는 알 수 없다.
이 모듈은 그 세 가지를 결정적으로 검사하고, 사람이 검토할 unified diff를 만든다.

검사는 임계값을 두지 않는다. "몇 줄 이상 바뀌면 거부" 같은 숫자는 근거 없는 정책이 되므로,
대신 구조적으로 설명 가능한 규칙만 둔다.

- 바뀌는 경로는 snapshot에 있는 Terraform 파일이어야 한다. 새 파일은 어느 리소스에 대한 수정인지
  snapshot과 대조할 수 없다.
- 바뀐 파일은 원본과 달라야 한다. 같은 내용을 올리면 PR은 빈 변경이 된다.
- 원본의 모든 `resource "<type>" "<name>"` 블록 헤더가 새 내용에도 남아 있어야 한다. 조치가
  리소스를 삭제하거나 이름을 바꾸면 apply가 리소스를 파괴·재생성하는데, 그것은 "최소 변경"이
  아니다.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping

from agent.runtime.github_tool import IaCDocument

_RESOURCE_HEADER = re.compile(r'^\s*resource\s+"([^"]+)"\s+"([^"]+)"\s*\{', re.MULTILINE)


class TerraformChangeError(ValueError):
    """The proposed change cannot be bound to the snapshot as a minimal remediation."""


def resource_block_headers(text: str) -> tuple[tuple[str, str], ...]:
    """Return every `resource "type" "name"` header in document order."""
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return tuple((kind, name) for kind, name in _RESOURCE_HEADER.findall(text))


def validate_terraform_changes(document: IaCDocument, changes: Mapping[str, str]) -> None:
    """Refuse a change set that is not a minimal edit of the snapshot's own files."""
    if not isinstance(document, IaCDocument):
        raise TypeError("document must be an IaCDocument")
    if not isinstance(changes, Mapping) or not changes:
        raise TerraformChangeError("changes must be a non-empty mapping")
    originals = dict(document.files)
    for path, contents in changes.items():
        original = originals.get(path)
        if original is None:
            raise TerraformChangeError(
                f"change path {path!r} is not a Terraform file in the assessed snapshot"
            )
        if not isinstance(contents, str) or not contents:
            raise TerraformChangeError(f"change contents for {path!r} must be a non-empty string")
        if contents == original:
            raise TerraformChangeError(f"change for {path!r} does not alter the file")
        missing = [
            header
            for header in resource_block_headers(original)
            if header not in set(resource_block_headers(contents))
        ]
        if missing:
            described = ", ".join(f"{kind}.{name}" for kind, name in missing)
            raise TerraformChangeError(
                f"change for {path!r} removes or renames resource blocks: {described}"
            )


def render_unified_diff(document: IaCDocument, changes: Mapping[str, str]) -> str:
    """Render one unified diff over the changed files, in path order.

    `validate_terraform_changes()`를 통과한 change set을 전제로 한다. 경로가 snapshot에 없으면
    원본을 빈 파일로 취급하지 않고 실패한다 — 빈 파일 대비 diff는 "새 파일"이라는 사실을 숨긴다.
    """
    if not isinstance(document, IaCDocument):
        raise TypeError("document must be an IaCDocument")
    originals = dict(document.files)
    chunks: list[str] = []
    for path in sorted(changes):
        original = originals.get(path)
        if original is None:
            raise TerraformChangeError(
                f"change path {path!r} is not a Terraform file in the assessed snapshot"
            )
        lines = difflib.unified_diff(
            original.splitlines(keepends=True),
            changes[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
        chunks.append("".join(_with_newline(line) for line in lines))
    return "".join(chunks)


def _with_newline(line: str) -> str:
    return line if line.endswith("\n") else line + "\n"
