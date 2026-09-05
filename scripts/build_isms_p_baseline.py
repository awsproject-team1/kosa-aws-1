#!/usr/bin/env python3
"""Derive the ISMS-P baseline registry from the local certification standard original.

ISMS-P는 고객이 올리는 문서가 아니다. 모든 고객에게 같은 인증기준이므로 **운영자가 한 번**
등록하고 bootstrap이 고객 파티션에 게시한다(ADR-0026). 이 스크립트가 그 등록의 유일한
생산 경로다 — 101개 인증기준 항목을 손으로 옮겨 적으면 어느 항목이 빠졌는지 아무도 모른다.

출력은 `fixtures/baselines/isms-p-2023/`의 네 파일이며 `load_rule_registry`가 그대로 읽는다.

- `sources.json`  : legacy Registry(`fixtures/rules/sources.json`)의 `isms-p-2023` 항목을 **그대로**
                    복사한다. 같은 Source가 두 Registry에서 다른 바이트로 게시되면 bootstrap이
                    "different immutable content"로 fail-closed한다.
- `controls.json` : 인증기준 항목마다 Control 하나(`ISMS-P-x.y.z`). 그 항목의 MANUAL Rule과, 그
                    항목을 근거로 인용하는 자동 판정 Rule을 함께 가리킨다 — Coverage가 "이 항목은
                    몇 개의 Rule로 얼마나 평가됐는가"를 말하는 근거다.
- `rules.isms-p.json` : 두 종류의 Rule.
    * 항목마다 MANUAL Rule 하나(`ISMSP-x.y.z`). 사람이 증적을 검토해 판정하는 좌표이며
      Bedrock·AWS·GitHub 어느 도구도 부르지 않는다(`ManualReviewEvaluator`).
    * Catalog의 자동 판정 가능 통제마다 Rule 하나(`ISMSP-<CONTROL_KEY>`). 그 통제가 근거가 되는
      인증기준 항목들을 `source_references`로 인용한다(`AUTOMATABLE_MAPPING`). 통제 하나에 Rule
      하나다 — 항목마다 Rule을 복제하면 같은 사실(예: SG ingress 개방)이 인용 항목 수만큼 점수에
      여러 번 들어간다.
- `profiles.json` : `profile-isms-p-baseline`. 위 Rule 전부, `ISMS_P` Segment 하나.

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
    MVP_CONTROL_CATALOG,
)
from packages.contracts import (  # noqa: E402
    ControlAutomationSupport,
    PolicySourceKind,
    RuleEvaluationType,
    RuleSeverity,
)

LEGACY_REGISTRY_DIR = REPO_ROOT / "fixtures" / "rules"
BASELINE_DIR = REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023"
DIGEST_SCRIPT = REPO_ROOT / "scripts" / "policy_source_digest.py"

SOURCE_ID = "isms-p-2023"
SOURCE_VERSION = "2023-10-31"
PROFILE_ID = "profile-isms-p-baseline"
#: `v1`은 MANUAL 101개만 담고 게시됐다. 자동 판정 Rule을 더한 판본은 새 version이어야 한다 — 게시된
#: Profile 판본 item은 불변이고, bootstrap은 current pointer만 옮긴다.
PROFILE_VERSION = "v2"
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

#: Catalog의 자동 판정 통제가 **근거가 되는** 인증기준 항목. 사람이 정한 매핑이고, 이 파일이 그
#: 결정의 기록이다(ADR-0026 §5). 기준은 "그 통제의 사실이 그 항목의 주요 확인사항 중 하나에 답하는가"
#: 이지 "그 항목을 전부 판정하는가"가 아니다 — 2.7.1 암호정책 적용은 확인사항이 여럿이고 저장·전송
#: 암호화는 그중 일부다. 그래서 자동 Rule이 있는 항목도 MANUAL Rule을 그대로 갖는다.
#:
#: 통제 하나에 Rule 하나이고 항목은 인용으로 붙는다. 항목마다 Rule을 만들면 같은 사실이 항목 수만큼
#: 점수에 들어간다(`readiness.py`: 같은 사실을 두 번 세지 않는다).
AUTOMATABLE_MAPPING: dict[str, tuple[str, ...]] = {
    "S3_BLOCK_PUBLIC_ACCESS": ("2.10.2", "2.10.3"),
    "S3_ENCRYPTION_AT_REST": ("2.7.1",),
    "S3_BUCKET_POLICY_RESTRICTED": ("2.6.2",),
    "S3_BUCKET_ACL_DISABLED": ("2.6.2",),
    "S3_TLS_ONLY": ("2.7.1", "2.10.4", "2.10.5"),
    "S3_SERVER_ACCESS_LOGGING": ("2.9.4",),
    "EC2_NO_PUBLIC_IP": ("2.6.1", "2.6.7", "2.10.2"),
    "EC2_EBS_ENCRYPTION": ("2.7.1",),
    "EC2_SG_INGRESS_RESTRICTED": ("2.6.1", "2.6.2", "2.6.6"),
    "RDS_NOT_PUBLIC": ("2.6.1", "2.6.4", "2.10.2"),
    "RDS_ACCESS_RESTRICTED": ("2.6.2", "2.6.4"),
    "RDS_ENCRYPTION_AT_REST": ("2.7.1",),
    "RDS_LOG_EXPORTS": ("2.9.4",),
    "ALB_HTTPS_ONLY": ("2.7.1", "2.10.3", "2.10.4", "2.10.5"),
    "ALB_ACCESS_LOGGING": ("2.9.4",),
}

#: 자동 판정 Rule의 실행 유형. legacy Rule과 같은 세 관점(IAC + AWS_ACTUAL + DRIFT)으로 평가된다.
AUTOMATABLE_EVALUATION_TYPE = RuleEvaluationType.HYBRID


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


def _require_mapping_targets(controls: list[Control]) -> None:
    """Every mapped item must exist in the original and every key must be an automatable control.

    매핑은 사람이 적은 표라 오타가 날 수 있다. 없는 항목을 인용하면 digest 조회에서 죽지만,
    KNOWN_UNSUPPORTED 통제를 매핑하면 실행 경로 없는 Rule이 조용히 게시된다 — 여기서 막는다.
    """
    known = {control.control_id for control in controls}
    for control_key, items in AUTOMATABLE_MAPPING.items():
        control = MVP_CONTROL_CATALOG.control(control_key)
        if control is None or control.automation_support is not ControlAutomationSupport.AVAILABLE:
            raise ValueError(f"{control_key} is not an automatable catalog control")
        if AUTOMATABLE_EVALUATION_TYPE not in control.supported_evaluation_types:
            raise ValueError(f"{control_key} does not support {AUTOMATABLE_EVALUATION_TYPE.value}")
        if not items or len(set(items)) != len(items):
            raise ValueError(f"{control_key} must cite distinct certification items")
        unknown = sorted(set(items) - known)
        if unknown:
            raise ValueError(f"{control_key} cites items not in the original: {unknown}")


def build(digest: ModuleType) -> dict[str, list[dict[str, object]]]:
    """Build the four registry documents. Deterministic for one original."""
    source = _legacy_source()
    rows = digest._xlsx_rows(digest._source_path(SOURCE_ID, SOURCE_VERSION))
    controls = read_controls(rows)
    if not controls:
        raise ValueError("no certification controls were found in the original")
    _require_mapping_targets(controls)

    def reference_for(control_id: str) -> dict[str, str]:
        locator = f"control/{control_id}"
        return {
            "source_id": SOURCE_ID,
            "source_version": SOURCE_VERSION,
            "locator": locator,
            "content_sha256": digest.reference_digest(SOURCE_ID, SOURCE_VERSION, locator),
        }

    by_id = {control.control_id: control for control in controls}
    rules: list[dict[str, object]] = []
    #: 항목 → 그 항목을 인용하는 자동 판정 Rule id. controls.json이 항목의 Rule 목록을 만들 때 쓴다.
    automatable_by_item: dict[str, list[str]] = {}

    for control in controls:
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
                "source_references": [reference_for(control.control_id)],
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

    for control_key in sorted(AUTOMATABLE_MAPPING):
        catalog_control = MVP_CONTROL_CATALOG.control(control_key)
        assert catalog_control is not None  # `_require_mapping_targets`가 확인했다.
        items = sorted(AUTOMATABLE_MAPPING[control_key], key=lambda i: by_id[i].sort_key)
        rule_id = f"ISMSP-{control_key}"
        for item in items:
            automatable_by_item.setdefault(item, []).append(rule_id)
        cited = ", ".join(f"{item} {by_id[item].name}" for item in items)
        rules.append(
            {
                "rule_id": rule_id,
                "version": SOURCE_VERSION,
                "title": f"ISMS-P {catalog_control.title}",
                "severity": catalog_control.default_severity.value,
                "applicable_phases": [
                    phase.value for phase in APPLICABLE_PHASES[AUTOMATABLE_EVALUATION_TYPE]
                ],
                "resource_types": list(catalog_control.supported_resource_types),
                "source_references": [reference_for(item) for item in items],
                "control_key": control_key,
                "control_catalog_version": CONTROL_CATALOG_VERSION,
                "evaluation_type": AUTOMATABLE_EVALUATION_TYPE.value,
                # 어느 항목의 어떤 확인사항에 답하는지. 항목 전체를 판정한다고 말하지 않는다.
                "applicability_semantics": (
                    f"ISMS-P {cited}의 확인사항 중 이 통제가 답하는 부분만 자동 판정한다. "
                    "항목의 나머지 확인사항은 MANUAL Rule로 사람이 검토한다."
                ),
                "required_evidence": list(catalog_control.baseline_required_evidence),
                "optional_evidence": list(catalog_control.baseline_optional_evidence),
                # rubric은 Catalog가 이미 정한 문장이다. 여기서 새로 쓰면 코드 술어와 어긋난다.
                "evaluation_rubric": catalog_control.description,
                "severity_guidance": catalog_control.severity_guidance,
            }
        )

    control_entries: list[dict[str, object]] = []
    for control in controls:
        implementing = [
            f"ISMSP-{control.control_id}",
            *automatable_by_item.get(control.control_id, []),
        ]
        control_entries.append(
            {
                "control_id": f"ISMS-P-{control.control_id}",
                "title": control.name,
                "source_reference": reference_for(control.control_id),
                "rule_references": [
                    {"rule_id": rule_id, "version": SOURCE_VERSION} for rule_id in implementing
                ],
            }
        )

    references = [{"rule_id": rule["rule_id"], "version": SOURCE_VERSION} for rule in rules]
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
    manual = sum(1 for rule in rules if rule["evaluation_type"] == RuleEvaluationType.MANUAL.value)
    summary = f"{manual} manual + {len(rules) - manual} automatable rules"
    if arguments.check:
        stale = check(documents)
        if stale:
            print("ISMS-P baseline registry is stale: " + ", ".join(stale))
            return 1
        print(f"OK: committed ISMS-P baseline matches the original ({summary}).")
        return 0
    written = write(documents)
    print(f"wrote {written} files to {BASELINE_DIR.relative_to(REPO_ROOT)} ({summary})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
