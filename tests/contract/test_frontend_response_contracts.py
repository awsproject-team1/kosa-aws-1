"""The frontend may only read response fields the backend contracts actually emit.

TypeScript cannot catch this class of defect: a hand-written `type` alias for a JSON response
is an assertion, not a check. A field that does not exist compiles, type-checks, builds, and
then reads `undefined` at runtime — which is how a Post-Deploy comparison table ended up
keying its rows on a `finding_id` that `FindingResolutionResult` never emits.

So the mapping is verified here instead, where the contracts live. The rule is **subset**:
reading fewer fields than a response carries is normal (a screen shows what it needs), but
declaring a field the contract does not emit is always wrong.

Request bodies are deliberately not covered. The handler validates them with an exact field
set (`set(body) != {...}` → 400), so a wrong request body fails loudly on the first call
rather than silently rendering nothing.
"""

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).parents[2]
FRONTEND = REPO_ROOT / "apps/frontend/src/main.tsx"

#: TypeScript response type in the frontend → (module, class) whose `to_dict()` produces it.
#: Only API-response types are listed. The redesigned SPA also declares internal view-model types
#: (Observer/QueueJob/PipelineStep/Session/Turn) that are not API responses and are not checked.
_RESPONSE_CONTRACTS = {
    "OrchestrationDecision": ("packages/contracts/orchestration.py", "OrchestrationDecision"),
    "Report": ("apps/backend/assessment/reporting.py", "AssessmentReport"),
    "UploadSession": ("apps/backend/api/policy_sources.py", "PolicySourceUploadSession"),
    "NormalizedDoc": ("packages/contracts/policy_ingestion.py", "NormalizedPolicyDocument"),
    "CandidatePage": ("apps/backend/api/policy_candidates.py", "PolicyCandidatePage"),
}

_TS_TYPE = "type {name} = {{"
_TS_FIELD = re.compile(r"(\w+)\s*:")


def _ts_type_fields(source: str, name: str) -> set[str]:
    """Return the top-level field names of one `type X = { … }` alias."""
    marker = _TS_TYPE.format(name=name)
    start = source.find(marker)
    if start < 0:
        raise AssertionError(f"frontend declares no response type named {name!r}")
    body_start = start + len(marker)
    depth = 1
    index = body_start
    while index < len(source) and depth:
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
        index += 1
    body = source[body_start : index - 1]
    # Only top-level members: drop nested object literals so `coverage: { … }` contributes
    # `coverage` and not its inner names.
    flattened, nesting = [], 0
    for character in body:
        if character == "{":
            nesting += 1
        elif character == "}":
            nesting -= 1
        elif nesting == 0:
            flattened.append(character)
    return {match.group(1) for match in _TS_FIELD.finditer("".join(flattened))}


def _to_dict_keys(relative_path: str, class_name: str) -> set[str]:
    """Return the literal keys a contract's `to_dict()` returns.

    Read from the AST rather than by constructing an instance: building a valid
    `AssessmentComparison` needs a whole comparison, and the keys are what the frontend
    consumes either way.
    """
    tree = ast.parse((REPO_ROOT / relative_path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for member in node.body:
            if not isinstance(member, ast.FunctionDef) or member.name != "to_dict":
                continue
            for statement in ast.walk(member):
                if isinstance(statement, ast.Return) and isinstance(statement.value, ast.Dict):
                    return {
                        key.value
                        for key in statement.value.keys
                        if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    }
        raise AssertionError(f"{class_name} has no to_dict() returning a dict literal")
    raise AssertionError(f"{relative_path} declares no class {class_name}")


class FrontendResponseContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.source = FRONTEND.read_text(encoding="utf-8")

    def test_every_frontend_response_type_reads_only_emitted_fields(self) -> None:
        for ts_type, (relative_path, class_name) in _RESPONSE_CONTRACTS.items():
            with self.subTest(response=ts_type, contract=class_name):
                declared = _ts_type_fields(self.source, ts_type)
                emitted = _to_dict_keys(relative_path, class_name)
                self.assertTrue(declared, f"{ts_type} declares no fields")
                self.assertLessEqual(
                    declared,
                    emitted,
                    f"{ts_type} reads {sorted(declared - emitted)} which {class_name} never emits",
                )

    def test_the_mapping_covers_every_response_type_the_frontend_declares(self) -> None:
        """모든 매핑된 API 응답 type이 실제로 SPA에 선언돼 있어야 한다.

        재설계된 SPA는 API 응답이 아닌 내부 view-model type(Observer/Session/Turn 등)도 선언한다.
        그것들은 이 검사 대상이 아니므로, '매핑된 응답 type이 모두 존재하는가'만 확인한다.
        """
        declared = set(re.findall(r"^type (\w+) = \{", self.source, flags=re.MULTILINE))
        self.assertLessEqual(set(_RESPONSE_CONTRACTS), declared)

    def test_a_field_no_contract_emits_is_detected(self) -> None:
        """이 검사가 실제로 무언가를 잡는지 확인한다 (통과만 하는 검사가 아니다)."""
        with self.assertRaisesRegex(AssertionError, "never emits"):
            declared = _ts_type_fields(
                "type Fake = { resource_id: string; not_a_contract_field: string };", "Fake"
            )
            emitted = _to_dict_keys(*_RESPONSE_CONTRACTS["Report"])
            self.assertLessEqual(
                declared, emitted, f"Fake reads {sorted(declared - emitted)} which never emits"
            )

    def test_assessment_confirmation_keeps_the_in_memory_access_token(self) -> None:
        """챗봇에서 Assessment를 시작해도 in-memory 토큰만 쓰고 페이지를 리로드하지 않는다."""
        self.assertIn("onAssessment(r.assessment_id)", self.source)
        self.assertNotIn("window.location.assign(`${window.location.pathname}?", self.source)


if __name__ == "__main__":
    unittest.main()
