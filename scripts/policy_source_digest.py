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
#: 검증 대상 Registry 전부. ISMS-P 기준선은 별도 디렉터리다 — legacy Registry는 세 관점으로
#: 평가되는 legacy Rule만 담는다는 계약이 있고(`fixtures/README.md`), 기준선의 MANUAL Rule을 거기
#: 섞으면 그 계약과 그것을 고정한 테스트가 함께 깨진다.
REGISTRY_DIRS = (REGISTRY_DIR, REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023")
SHEET_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

# (source_id, source_version) -> 로컬 원문 파일명. 원문 자체는 저장소에 없다.
# 같은 Source의 여러 판본이 공존할 수 있으므로 version까지 key에 넣는다.
SOURCE_FILES = {
    ("internal-cloud-security-checklist", "2026-08-24"): "cloud-security-checklist.md",
    ("isms-p-2023", "2023-10-31"): "isms-p-2023-10-31.xlsx",
}


class PolicySourceUnavailableError(RuntimeError):
    """Raised when a policy original is not present in the local checkout."""


class UnknownPolicySourceError(RuntimeError):
    """Raised when a registry source has no local original mapping."""


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def source_digest(source_id: str, version: str) -> str:
    """Digest of one approved original version, used as `PolicySource.content_sha256`."""
    return hashlib.sha256(_source_path(source_id, version).read_bytes()).hexdigest()


def excerpt(source_id: str, version: str, locator: str) -> str:
    """Return the normalized excerpt a `SourceReference` points at in that source version."""
    path = _source_path(source_id, version)
    if path.suffix == ".md":
        return _checklist_excerpt(path, locator)
    if path.suffix == ".xlsx":
        return _isms_p_excerpt(path, locator)
    raise UnknownPolicySourceError(f"no excerpt parser for policy original {path.name!r}")


def reference_digest(source_id: str, version: str, locator: str) -> str:
    """Digest of one excerpt, used as `SourceReference.content_sha256`."""
    return sha256_of_text(excerpt(source_id, version, locator))


def _source_path(source_id: str, version: str) -> Path:
    try:
        path = POLICIES_LOCAL / SOURCE_FILES[(source_id, version)]
    except KeyError:
        raise UnknownPolicySourceError(
            f"unknown policy source {source_id}@{version}; register it in SOURCE_FILES"
        ) from None
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
    part, item = match.group("part"), match.group("item")
    start = re.compile(rf"^- \[ \] \*\*{re.escape(item)}\.\*\*")
    boundary = re.compile(r"^(- \[ \] \*\*|#{1,4} |---$)")

    lines = _part_lines(path.read_text(encoding="utf-8").splitlines(), part)
    for index, line in enumerate(lines):
        if start.match(line):
            block = [line]
            for following in lines[index + 1 :]:
                if boundary.match(following):
                    break
                block.append(following)
            return _normalize(block)
    raise KeyError(f"checklist item {item!r} not found in part {part} of {path.name}")


def _part_lines(lines: list[str], part: str) -> list[str]:
    """Restrict the scan to one `# Part N` section.

    항목 번호는 Part 사이에서 충돌할 수 있으므로 locator의 part 구획을 실제로 강제한다.
    """
    heading = re.compile(rf"^# Part {re.escape(part)}")
    other = re.compile(r"^# Part \d")
    for index, line in enumerate(lines):
        if heading.match(line):
            section = [line]
            for following in lines[index + 1 :]:
                if other.match(following):
                    break
                section.append(following)
            return section
    raise KeyError(f"checklist part {part!r} not found")


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


def _registry_references() -> tuple[dict[tuple[str, str], str], list[tuple[str, str, str, str]]]:
    """Return committed digests keyed by (source_id, version) plus pinned references.

    두 Registry(legacy `fixtures/rules/`, ISMS-P 기준선 `fixtures/baselines/isms-p-2023/`)를 함께
    읽는다. 같은 Source가 양쪽에 선언될 수 있으며(기준선은 legacy의 `isms-p-2023` 항목을 그대로
    복사한다), 그때 digest가 다르면 bootstrap이 fail-closed할 내용이므로 여기서도 거부한다.
    """
    source_digests: dict[tuple[str, str], str] = {}
    for directory in REGISTRY_DIRS:
        sources = json.loads((directory / "sources.json").read_text(encoding="utf-8"))
        seen_here: set[tuple[str, str]] = set()
        for entry in sources:
            key = (entry["source_id"], entry["version"])
            if key in seen_here:
                raise ValueError(f"duplicate policy source {key[0]}@{key[1]} in sources.json")
            seen_here.add(key)
            committed = source_digests.setdefault(key, entry["content_sha256"])
            if committed != entry["content_sha256"]:
                raise ValueError(
                    f"policy source {key[0]}@{key[1]} is declared with different digests"
                )

    references: list[tuple[str, str, str, str]] = []

    def add(reference: dict[str, str]) -> None:
        references.append(
            (
                reference["source_id"],
                reference["source_version"],
                reference["locator"],
                reference["content_sha256"],
            )
        )

    for directory in REGISTRY_DIRS:
        for rule_file in sorted(directory.glob("rules.*.json")):
            for rule in json.loads(rule_file.read_text(encoding="utf-8")):
                for reference in rule["source_references"]:
                    add(reference)
        for control in json.loads((directory / "controls.json").read_text(encoding="utf-8")):
            add(control["source_reference"])
    return source_digests, sorted(set(references))


def _print_digests() -> int:
    source_digests, references = _registry_references()
    for source_id, version in sorted(source_digests):
        print(f"source  {source_id}@{version:24s} {source_digest(source_id, version)}")
    for source_id, version, locator, _ in references:
        print(
            f"ref     {source_id}@{version}#{locator:24s} "
            f"{reference_digest(source_id, version, locator)}"
        )
    return 0


def _verify() -> int:
    """Verify every available original. 원문이 없는 source만 건너뛰고 나머지는 반드시 검증한다."""
    source_digests, references = _registry_references()
    failures: list[str] = []
    skipped: set[tuple[str, str]] = set()
    checked_sources = 0
    checked_references = 0

    for source_id, version in sorted(source_digests):
        committed = source_digests[(source_id, version)]
        try:
            actual = source_digest(source_id, version)
        except PolicySourceUnavailableError:
            skipped.add((source_id, version))
            continue
        checked_sources += 1
        if actual != committed:
            failures.append(f"source {source_id}@{version}: committed {committed}, actual {actual}")

    for source_id, version, locator, committed in references:
        if (source_id, version) not in source_digests:
            failures.append(
                f"reference {source_id}@{version}#{locator}: pinned to an undeclared source version"
            )
            continue
        if (source_id, version) in skipped:
            continue
        actual = reference_digest(source_id, version, locator)
        checked_references += 1
        if actual != committed:
            failures.append(
                f"reference {source_id}@{version}#{locator}: committed {committed}, actual {actual}"
            )

    for source_id, version in sorted(skipped):
        print(f"Skipped {source_id}@{version}: policy original not available locally.")
    if failures:
        print("Policy source digests do NOT match the local originals:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print(f"OK: {checked_sources} sources and {checked_references} references match.")
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
        # `--print`만 이 경로로 온다. `--verify`는 source별로 건너뛰고 나머지를 계속 검증한다.
        print(f"Skipped: {error}")
        print("정책 원문은 저장소에 없다 (ADR-0004). 검증은 원문 보유자만 수행한다.")
        return 0
    except UnknownPolicySourceError as error:
        print(f"Error: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
