#!/usr/bin/env python3
"""Derive and verify `SourceReference` digests from the local policy originals.

정책 원문은 저장소에 커밋하지 않는다 (ADR-0004). 저장소에는 Rule 정의와 locator, 그리고
원문 발췌의 SHA-256만 남는다. 이 스크립트는 로컬 `policies-local/` 원문에서 그 digest를
재계산해, 커밋된 Registry가 실제 원문의 어느 부분을 가리키는지 검증할 수 있게 한다.

원문이 없는 환경(공개 clone, CI)에서는 검증을 건너뛰고 정상 종료한다.

    python scripts/policy_source_digest.py --print     # locator별 digest 출력
    python scripts/policy_source_digest.py --verify    # Registry digest와 대조
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

REPO_ROOT = Path(__file__).resolve().parent.parent
POLICIES_LOCAL = REPO_ROOT / "policies-local"
REGISTRY_DIR = REPO_ROOT / "fixtures" / "rules"
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# source_id -> 로컬 원문 파일명. 원문 자체는 저장소에 없다.
SOURCE_FILES = {
    "internal-cloud-security-checklist": "cloud-security-checklist.md",
    "isms-p-2023": "isms-p-2023-10-31.xlsx",
}


class PolicySourceUnavailableError(RuntimeError):
    """Raised when a policy original is not present in the local checkout."""


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_digest(source_id: str) -> str:
    """Digest of the whole approved original, used as `PolicySource.content_sha256`."""
    return hashlib.sha256(_source_path(source_id).read_bytes()).hexdigest()


def excerpt(source_id: str, locator: str) -> str:
    """Return the normalized original excerpt a `SourceReference` locator points at."""
    if source_id == "internal-cloud-security-checklist":
        return _checklist_excerpt(_source_path(source_id), locator)
    if source_id == "isms-p-2023":
        return _isms_p_excerpt(_source_path(source_id), locator)
    raise KeyError(f"unknown policy source {source_id!r}")


def reference_digest(source_id: str, locator: str) -> str:
    """Digest of one excerpt, used as `SourceReference.content_sha256`."""
    return sha256_of_text(excerpt(source_id, locator))


def _source_path(source_id: str) -> Path:
    try:
        path = POLICIES_LOCAL / SOURCE_FILES[source_id]
    except KeyError:
        raise KeyError(f"unknown policy source {source_id!r}") from None
    if not path.is_file():
        raise PolicySourceUnavailableError(f"policy original not available: {path}")
    return path


def _normalize(lines: list[str]) -> str:
    """Trailing whitespace와 앞뒤 빈 줄만 제거해 줄바꿈 차이에 안정적인 발췌를 만든다."""
    stripped = [line.rstrip() for line in lines]
    while stripped and not stripped[0]:
        stripped.pop(0)
    while stripped and not stripped[-1]:
        stripped.pop()
    return "\n".join(stripped)


def _checklist_excerpt(path: Path, locator: str) -> str:
    """`part2/5.1-B` 형식의 locator가 가리키는 체크리스트 항목 블록을 발췌한다."""
    match = re.fullmatch(r"part(?P<part>[12])/(?P<item>[0-9.]+-[A-Z])", locator)
    if match is None:
        raise ValueError(f"unsupported checklist locator {locator!r}")
    item = match.group("item")
    start = re.compile(rf"^- \[ \] \*\*{re.escape(item)}\.\*\*")
    boundary = re.compile(r"^(- \[ \] \*\*|#{1,4} |---$)")

    lines = path.read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(lines):
        if start.match(line):
            block = [line]
            for following in lines[index + 1 :]:
                if boundary.match(following):
                    break
                block.append(following)
            return _normalize(block)
    raise KeyError(f"checklist item {item!r} not found in {path.name}")


def _isms_p_excerpt(path: Path, locator: str) -> str:
    """`control/2.6.2` 형식의 locator가 가리키는 ISMS-P 인증기준 행을 발췌한다."""
    match = re.fullmatch(r"control/(?P<control>\d+\.\d+\.\d+)", locator)
    if match is None:
        raise ValueError(f"unsupported ISMS-P locator {locator!r}")
    control_id = match.group("control")

    for row in _xlsx_rows(path):
        cells = [cell.strip() for cell in row]
        # 절의 첫 항목 행은 분야 번호와 분야명 셀이 앞에 붙으므로 항목 번호 위치를 찾는다.
        if control_id not in cells:
            continue
        start = cells.index(control_id)
        # 인증기준 식별에 필요한 (항목 번호, 항목명, 상세내용)만 취한다.
        return _normalize([" | ".join(cells[start : start + 3])])
    raise KeyError(f"ISMS-P control {control_id!r} not found in {path.name}")


def _xlsx_rows(path: Path) -> list[list[str]]:
    """openpyxl 없이 표준 라이브러리만으로 워크시트 셀 값을 읽는다."""
    with zipfile.ZipFile(path) as archive:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            table = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = [
                "".join(node.text or "" for node in item.iter(f"{SHEET_NS}t"))
                for item in table.findall(f"{SHEET_NS}si")
            ]
        sheet_names = [
            name for name in archive.namelist() if name.startswith("xl/worksheets/sheet")
        ]
        rows: list[list[str]] = []
        for sheet_name in sorted(sheet_names):
            sheet = ElementTree.fromstring(archive.read(sheet_name))
            for row in sheet.iter(f"{SHEET_NS}row"):
                cells: list[str] = []
                for cell in row.findall(f"{SHEET_NS}c"):
                    value = cell.find(f"{SHEET_NS}v")
                    if value is None or value.text is None:
                        continue
                    if cell.get("t") == "s":
                        cells.append(shared[int(value.text)].replace("\n", " "))
                    else:
                        cells.append(value.text)
                if cells:
                    rows.append(cells)
        return rows


def _registry_references() -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    """Return committed source digests and (source_id, locator, digest) references."""
    sources = json.loads((REGISTRY_DIR / "sources.json").read_text(encoding="utf-8"))
    source_digests = {entry["source_id"]: entry["content_sha256"] for entry in sources}

    references: list[tuple[str, str, str]] = []
    for rule_file in sorted(REGISTRY_DIR.glob("rules.*.json")):
        for rule in json.loads(rule_file.read_text(encoding="utf-8")):
            for reference in rule["source_references"]:
                references.append(
                    (reference["source_id"], reference["locator"], reference["content_sha256"])
                )
    for control in json.loads((REGISTRY_DIR / "controls.json").read_text(encoding="utf-8")):
        reference = control["source_reference"]
        references.append(
            (reference["source_id"], reference["locator"], reference["content_sha256"])
        )
    return source_digests, sorted(set(references))


def _print_digests() -> int:
    source_digests, references = _registry_references()
    for source_id in sorted(source_digests):
        print(f"source  {source_id:38s} {source_digest(source_id)}")
    for source_id, locator, _ in references:
        print(f"ref     {source_id}#{locator:24s} {reference_digest(source_id, locator)}")
    return 0


def _verify() -> int:
    source_digests, references = _registry_references()
    failures: list[str] = []
    for source_id, committed in sorted(source_digests.items()):
        actual = source_digest(source_id)
        if actual != committed:
            failures.append(f"source {source_id}: committed {committed}, actual {actual}")
    for source_id, locator, committed in references:
        actual = reference_digest(source_id, locator)
        if actual != committed:
            failures.append(
                f"reference {source_id}#{locator}: committed {committed}, actual {actual}"
            )
    if failures:
        print("Policy source digests do NOT match the local originals:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {len(source_digests)} sources and {len(references)} references match.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--print", dest="print_digests", action="store_true")
    group.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    try:
        return _print_digests() if args.print_digests else _verify()
    except PolicySourceUnavailableError as error:
        print(f"Skipped: {error}")
        print("정책 원문은 저장소에 없다 (ADR-0004). 검증은 원문 보유자만 수행한다.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
