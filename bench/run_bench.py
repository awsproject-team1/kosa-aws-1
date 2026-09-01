"""Run opt-in Bedrock model measurements for the four governance agent roles."""

from __future__ import annotations

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bench.cases import CASES_BY_ROLE  # noqa: E402
from bench.config import (  # noqa: E402
    DEFAULT_REGION,
    DEFAULT_RUNS,
    ROLE_CANDIDATES,
    ROLE_NAMES,
    ModelCandidate,
)
from bench.runner import (  # noqa: E402
    BedrockConverseClient,
    discover_text_models,
    execute_case,
    write_reports,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure Bedrock candidates with role-specific synthetic governance cases."
    )
    parser.add_argument(
        "role",
        choices=("all", *ROLE_CANDIDATES),
        help="Agent role to evaluate, or all four roles.",
    )
    parser.add_argument(
        "--runs",
        type=positive_int,
        default=DEFAULT_RUNS,
        help=f"Repeated executions per candidate/case (default: {DEFAULT_RUNS}).",
    )
    parser.add_argument(
        "--region",
        default=os.environ.get("AWS_REGION", DEFAULT_REGION),
        help=f"Bedrock region (default: AWS_REGION or {DEFAULT_REGION}).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "bench" / "results",
        help="Git-ignored directory for sanitized result files.",
    )
    parser.add_argument(
        "--all-available-models",
        action="store_true",
        help=(
            "Query Bedrock and evaluate every active on-demand Text-to-Text foundation model "
            "visible to the account for every selected role."
        ),
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=8,
        help="Maximum concurrent Bedrock calls (default: 8).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Required to make paid Bedrock Converse API calls.",
    )
    return parser.parse_args()


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def selected_roles(role: str) -> tuple[str, ...]:
    return tuple(ROLE_CANDIDATES) if role == "all" else (role,)


def planned_call_count(
    roles: tuple[str, ...],
    runs: int,
    candidates_by_role: dict[str, tuple[ModelCandidate, ...]],
) -> int:
    return sum(len(candidates_by_role[role]) * len(CASES_BY_ROLE[role]) * runs for role in roles)


def load_local_env(path: Path) -> None:
    """Load simple missing KEY=VALUE entries without logging names or values."""
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and value:
            os.environ.setdefault(key, value)


def main() -> int:
    args = parse_args()
    load_local_env(ROOT / ".env")
    roles = selected_roles(args.role)
    if args.all_available_models:
        discovered = discover_text_models(args.region)
        if not discovered:
            print("Bedrock returned no active on-demand Text-to-Text foundation models.")
            return 1
        candidates_by_role = {role: discovered for role in roles}
    else:
        candidates_by_role = {role: ROLE_CANDIDATES[role] for role in roles}

    call_count = planned_call_count(roles, args.runs, candidates_by_role)
    print(f"Planned Bedrock Converse calls: {call_count}")
    for role in roles:
        candidates = ", ".join(candidate.model_id for candidate in candidates_by_role[role])
        print(f"- {ROLE_NAMES[role]}: {candidates}")

    if not args.execute:
        print("Dry run only. Add --execute to make paid API calls.")
        return 0

    if not (
        os.environ.get("AWS_BEARER_TOKEN_BEDROCK")
        or os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("AWS_PROFILE")
    ):
        print(
            "No explicit Bedrock credential environment variable found. boto3 will still try its "
            "standard credential chain; no credential value is printed."
        )

    client = BedrockConverseClient(args.region)
    work_items = [
        (role, case, candidate, run_number)
        for role in roles
        for candidate in candidates_by_role[role]
        for case in CASES_BY_ROLE[role]
        for run_number in range(1, args.runs + 1)
    ]
    results = []
    with ThreadPoolExecutor(max_workers=args.max_workers) as executor:
        futures = {
            executor.submit(execute_case, client, role, case, candidate, run_number): (
                role,
                case,
                candidate,
            )
            for role, case, candidate, run_number in work_items
        }
        for future in as_completed(futures):
            role, case, candidate = futures[future]
            result = future.result()
            results.append(result)
            outcome = "valid" if result.valid else result.error_kind or "invalid output"
            print(f"{ROLE_NAMES[role]} | {candidate.label} | {case.case_id} | {outcome}")

    results.sort(key=lambda item: (item.role, item.model_id, item.case_id, item.run_number))
    json_path, markdown_path = write_reports(results, args.output_dir)
    print(f"Sanitized JSON: {json_path}")
    print(f"Selection report: {markdown_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
