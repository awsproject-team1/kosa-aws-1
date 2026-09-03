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
_RESPONSE_CONTRACTS = {
    "Deployment": ("apps/backend/api/deployments.py", "DeploymentView"),
    "Comparison": ("packages/contracts/assessments.py", "AssessmentComparison"),
    "FindingResolution": ("packages/contracts/assessments.py", "FindingResolutionResult"),
    "Coverage": ("packages/contracts/assessments.py", "AssessmentCoverage"),
    "ReadinessScore": ("packages/contracts/assessments.py", "ReadinessScore"),
    "Finding": ("packages/contracts/assessments.py", "Finding"),
    "Result": ("packages/contracts/assessments.py", "EvaluationResult"),
    "RemediationDecision": ("packages/contracts/remediation_policy.py", "RemediationDecision"),
    "RemediationStart": ("packages/contracts/remediation.py", "RemediationStartResponse"),
    "Report": ("apps/backend/assessment/reporting.py", "AssessmentReport"),
    "Suppression": ("apps/backend/policy/remediation.py", "FindingSuppression"),
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
        """새 응답 type을 추가하고 이 표에 넣지 않으면 검사에서 조용히 빠진다."""
        declared = set(re.findall(r"^type (\w+) = \{", self.source, flags=re.MULTILINE))

        self.assertEqual(declared, set(_RESPONSE_CONTRACTS))

    def test_a_field_no_contract_emits_is_detected(self) -> None:
        """이 검사가 실제로 무언가를 잡는지 확인한다 (통과만 하는 검사가 아니다)."""
        with self.assertRaisesRegex(AssertionError, "never emits"):
            declared = _ts_type_fields(
                "type Fake = { resource_id: string; not_a_contract_field: string };", "Fake"
            )
            emitted = _to_dict_keys(*_RESPONSE_CONTRACTS["FindingResolution"])
            self.assertLessEqual(
                declared, emitted, f"Fake reads {sorted(declared - emitted)} which never emits"
            )

    def test_login_round_trip_restores_the_requested_screen(self) -> None:
        self.assertIn(
            "sessionStorage.setItem(returnToKey, `${returnTo.pathname}${returnTo.search}${returnTo.hash}`)",
            self.source,
        )
        self.assertIn('history.replaceState({}, "", destination)', self.source)
        self.assertIn("setRoute(routeFromLocation());", self.source)

    def test_starting_an_assessment_keeps_the_in_memory_access_token(self) -> None:
        self.assertIn("onStarted(result.assessment_id);", self.source)
        self.assertIn('history.pushState({}, "",', self.source)
        self.assertNotIn(
            "window.location.assign(`${window.location.pathname}?assessment_id=", self.source
        )


if __name__ == "__main__":
    unittest.main()
