#!/usr/bin/env python3
"""Derive the ISMS-P baseline registry from the local certification standard original.

ISMS-P는 고객이 올리는 문서가 아니다. 모든 고객에게 같은 인증기준이므로 **운영자가 한 번**
등록하고 bootstrap이 고객 파티션에 게시한다(ADR-0026). 이 스크립트가 그 등록의 유일한
생산 경로다 — 101개 인증기준 항목을 손으로 옮겨 적으면 어느 항목이 빠졌는지 아무도 모른다.

출력은 `fixtures/baselines/isms-p-2023/`의 네 파일이며 `load_rule_registry`가 그대로 읽는다.

- `sources.json`  : legacy Registry(`fixtures/rules/sources.json`)의 `isms-p-2023` 항목을 **그대로**
                    복사한다. 같은 Source가 두 Registry에서 다른 바이트로 게시되면 bootstrap이
                    "different immutable content"로 fail-closed한다.
- `controls.json` : 인증기준 항목마다 Control 하나(`ISMS-P-x.y.z`).
- `rules.isms-p.json` : 항목마다 MANUAL Rule 하나(`ISMSP-x.y.z`). 사람이 증적을 검토해 판정하는
                    좌표이며 Bedrock·AWS·GitHub 어느 도구도 부르지 않는다(`ManualReviewEvaluator`).
- `profiles.json` : `profile-isms-p-baseline@v1`. 101개 Rule 전부, `ISMS_P` Segment 하나.

정책 원문은 저장소에 남기지 않는다(ADR-0004). 파일에 들어가는 것은 항목 번호·항목명·분야명과
발췌 digest뿐이고, digest는 `scripts/policy_source_digest.py`와 같은 발췌 규칙으로 계산한다.

    python scripts/build_isms_p_baseline.py          # 생성
    python scripts/build_isms_p_baseline.py --check  # 커밋본과 대조 (원문이 없으면 건너뜀)
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.backend.policy.authoring.rule_builder import APPLICABLE_PHASES  # noqa: E402
from apps.backend.policy.control_catalog import (  # noqa: E402 - repo root must precede.
    CONTROL_CATALOG_VERSION,
    GOVERNANCE_ASSESSMENT_RESOURCE_TYPE,
    MANUAL_CONTROL_KEY,
)
from packages.contracts import PolicySourceKind, RuleEvaluationType, RuleSeverity  # noqa: E402

LEGACY_REGISTRY_DIR = REPO_ROOT / "fixtures" / "rules"
BASELINE_DIR = REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023"
DIGEST_SCRIPT = REPO_ROOT / "scripts" / "policy_source_digest.py"

SOURCE_ID = "isms-p-2023"
SOURCE_VERSION = "2023-10-31"
PROFILE_ID = "profile-isms-p-baseline"
PROFILE_VERSION = "v1"
RULE_FILE = "rules.isms-p.json"

#: 인증기준 항목 번호. `1.1.1` 꼴만 항목이고 `1.1.`은 분야, `1.`은 영역이다.
_CONTROL_ID = re.compile(r"^\d+\.\d+\.\d+$")
_SECTION_ID = re.compile(r"^\d+\.\d+\.$")
_PART_HEADING = re.compile(r"^(?P<part>\d)\.\s*(?P<name>.+?)\s*\(")

#: MANUAL 통제의 severity. Catalog의 `ORGANIZATIONAL_CONTROL_MANUAL_REVIEW.default_severity`와
#: 같은 값이다 — authoring이 만드는 MANUAL Rule과 같은 등급이어야 한 화면에서 비교가 된다.
#: 검토되지 않았다는 사실이 심각도를 낮추지 않지만, 인증기준 항목 사이의 등급 차이는 이 Registry가
#: 아니라 심사원이 정할 일이므로 여기서 항목별로 다르게 매기지 않는다.
MANUAL_SEVERITY = RuleSeverity.MEDIUM


def _digest_module() -> ModuleType:
    """Load the digest script as a module so both tools share one excerpt rule."""
    spec = importlib.util.spec_from_file_location("policy_source_digest", DIGEST_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Control:
    __slots__ = ("control_id", "name", "part_name", "section_name")

    def __init__(self, control_id: str, name: str, part_name: str, section_name: str) -> None:
        self.control_id = control_id
        self.name = name
        self.part_name = part_name
        self.section_name = section_name

    @property
    def sort_key(self) -> tuple[int, ...]:
        return tuple(int(part) for part in self.control_id.split("."))


def read_controls(rows: list[list[str]]) -> list[Control]:
    """Walk the worksheet rows and return every certification control, in standard order.

    행 모양은 셋이다. 영역 머리글(`1. 관리체계 수립 및 운영(16개)/...`), 분야가 시작되는 항목 행
    (`1.1.`, `관리체계 기반 마련`, `1.1.1`, `경영진의 참여`, ...), 그리고 분야 안의 후속 항목 행
    (`1.1.2`, `최고책임자의 지정`, ...). 항목의 주요 확인사항이 여러 행으로 이어지지만 그것은
    항목 번호 없는 행이라 여기서 건너뛴다 — locator는 항목 단위다(`control/x.y.z`).
    """
    controls: list[Control] = []
    part_name = ""
    section_name = ""
    for row in rows:
        cells = [cell.strip() for cell in row if cell.strip()]
        if not cells:
            continue
        heading = _PART_HEADING.match(cells[0])
        if heading is not None and len(cells) == 1:
            part_name = heading.group("name").strip()
            continue
        index = 0
        if _SECTION_ID.match(cells[0]) and len(cells) >= 2:
            section_name = cells[1]
            index = 2
        if index < len(cells) and _CONTROL_ID.match(cells[index]):
            if index + 1 >= len(cells):
                raise ValueError(f"control {cells[index]} has no name cell")
            if not part_name or not section_name:
                raise ValueError(f"control {cells[index]} appears before its headings")
            controls.append(Control(cells[index], cells[index + 1], part_name, section_name))
    controls.sort(key=lambda control: control.sort_key)
    seen: set[str] = set()
    for control in controls:
        if control.control_id in seen:
            raise ValueError(f"control {control.control_id} appears twice in the original")
        seen.add(control.control_id)
    return controls


def _legacy_source() -> dict[str, object]:
    entries = json.loads((LEGACY_REGISTRY_DIR / "sources.json").read_text(encoding="utf-8"))
    for entry in entries:
        if entry.get("source_id") == SOURCE_ID and entry.get("version") == SOURCE_VERSION:
            return dict(entry)
    raise LookupError(f"{SOURCE_ID}@{SOURCE_VERSION} is not declared in the legacy registry")


def build(digest: ModuleType) -> dict[str, list[dict[str, object]]]:
    """Build the four registry documents. Deterministic for one original."""
    source = _legacy_source()
    rows = digest._xlsx_rows(digest._source_path(SOURCE_ID, SOURCE_VERSION))
    controls = read_controls(rows)
    if not controls:
        raise ValueError("no certification controls were found in the original")

    rules: list[dict[str, object]] = []
    control_entries: list[dict[str, object]] = []
    references: list[dict[str, str]] = []
    for control in controls:
        locator = f"control/{control.control_id}"
        reference = {
            "source_id": SOURCE_ID,
            "source_version": SOURCE_VERSION,
            "locator": locator,
            "content_sha256": digest.reference_digest(SOURCE_ID, SOURCE_VERSION, locator),
        }
        rule_id = f"ISMSP-{control.control_id}"
        rules.append(
            {
                "rule_id": rule_id,
                "version": SOURCE_VERSION,
                "title": f"ISMS-P {control.control_id} {control.name}",
                "severity": MANUAL_SEVERITY.value,
                "applicable_phases": [
                    phase.value for phase in APPLICABLE_PHASES[RuleEvaluationType.MANUAL]
                ],
                "resource_types": [GOVERNANCE_ASSESSMENT_RESOURCE_TYPE],
                "source_references": [reference],
                "control_key": MANUAL_CONTROL_KEY,
                "control_catalog_version": CONTROL_CATALOG_VERSION,
                "evaluation_type": RuleEvaluationType.MANUAL.value,
                # 분야명은 원문 발췌가 아니라 목차다. 어느 영역·분야의 통제인지 화면이 말할 수
                # 있게 남기되, 상세내용·확인사항 문장은 싣지 않는다(ADR-0004).
                "applicability_semantics": (
                    f"ISMS-P {control.part_name} > {control.section_name}. "
                    "인증기준 항목에 대한 증적을 사람이 검토해 판정한다."
                ),
            }
        )
        control_entries.append(
            {
                "control_id": f"ISMS-P-{control.control_id}",
                "title": control.name,
                "source_reference": reference,
                "rule_references": [{"rule_id": rule_id, "version": SOURCE_VERSION}],
            }
        )
        references.append({"rule_id": rule_id, "version": SOURCE_VERSION})

    profile = {
        "policy_profile_id": PROFILE_ID,
        "version": PROFILE_VERSION,
        "rule_references": references,
        "segments": [
            {
                "kind": PolicySourceKind.ISMS_P.value,
                "source_id": SOURCE_ID,
                "source_version": SOURCE_VERSION,
                "rule_references": references,
            }
        ],
    }
    return {
        "sources.json": [source],
        "controls.json": control_entries,
        RULE_FILE: rules,
        "profiles.json": [profile],
    }


def _render(document: list[dict[str, object]]) -> str:
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def write(documents: dict[str, list[dict[str, object]]]) -> int:
    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    for name, document in documents.items():
        (BASELINE_DIR / name).write_text(_render(document), encoding="utf-8")
    return len(documents)


def check(documents: dict[str, list[dict[str, object]]]) -> list[str]:
    """Return the committed files that differ from a fresh build."""
    stale: list[str] = []
    for name, document in documents.items():
        path = BASELINE_DIR / name
        if not path.is_file() or path.read_text(encoding="utf-8") != _render(document):
            stale.append(name)
    return stale


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Compare the committed baseline with a fresh build instead of writing",
    )
    arguments = parser.parse_args(argv)
    digest = _digest_module()
    try:
        documents = build(digest)
    except digest.PolicySourceUnavailableError as error:
        print(f"Skipped: {error}")
        print("정책 원문은 저장소에 없다 (ADR-0004). 생성과 대조는 원문 보유자만 수행한다.")
        return 0
    rules = documents[RULE_FILE]
    if arguments.check:
        stale = check(documents)
        if stale:
            print("ISMS-P baseline registry is stale: " + ", ".join(stale))
            return 1
        print(f"OK: committed ISMS-P baseline matches the original ({len(rules)} controls).")
        return 0
    written = write(documents)
    print(f"wrote {written} files to {BASELINE_DIR.relative_to(REPO_ROOT)} ({len(rules)} controls)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
