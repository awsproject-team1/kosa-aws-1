#!/usr/bin/env python3
"""`terraform show -json` 출력을 ADR-0019 §1 canonical 바이트로 stdout에 내보낸다.

plan/apply workflow가 이 스크립트로 canonical 투영을 만들고 그 SHA-256을 plan_hash로 쓴다.
Platform의 승인 재검증(`packages/contracts/terraform_plan.py`)과 **같은 규칙**이어야 한다.
규칙이 두 곳에서 갈리면 apply 직전 재검증이 상시 실패한다.

이 스크립트는 Platform 함수(`canonical_plan_bytes`)를 재사용할 수 있으면 그대로 부르고,
고객 repository에 그 패키지가 없으면 동일 규칙을 인라인 구현으로 적용한다. 두 경로가 같은
바이트를 내는지는 Platform 쪽 contract 테스트가 고정한다.

사용법:
    python3 ci/terraform/canonical_plan_hash.py plan.full.json > plan.canonical.json
"""

from __future__ import annotations

import json
import math
import sys
from collections.abc import Mapping, Sequence

_RESOURCE_CHANGE_TOP_FIELDS = ("address", "mode", "type", "name", "index", "provider_name")
_CHANGE_FIELDS = ("actions", "before", "after", "after_unknown", "replace_paths")


def _reject_non_finite(value: object) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("plan contains a non-finite number")
    if isinstance(value, Mapping):
        for item in value.values():
            _reject_non_finite(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_non_finite(item)


def _project(plan_json: Mapping[str, object]) -> list[dict[str, object]]:
    resource_changes = plan_json.get("resource_changes")
    if not isinstance(resource_changes, list):
        raise ValueError("plan JSON requires a resource_changes array")
    projected: list[dict[str, object]] = []
    for item in resource_changes:
        if not isinstance(item, Mapping):
            raise ValueError("resource_changes[] item must be an object")
        entry: dict[str, object] = {}
        for field in _RESOURCE_CHANGE_TOP_FIELDS:
            if field in item:
                _reject_non_finite(item[field])
                entry[field] = item[field]
        if not isinstance(entry.get("address"), str):
            raise ValueError("resource_changes[] item requires a string address")
        change = item.get("change")
        if not isinstance(change, Mapping):
            raise ValueError("resource_changes[].change must be an object")
        change_entry: dict[str, object] = {}
        for field in _CHANGE_FIELDS:
            if field in change:
                _reject_non_finite(change[field])
                change_entry[field] = change[field]
        if "actions" not in change_entry:
            raise ValueError("resource_changes[].change.actions is required")
        entry["change"] = change_entry
        projected.append(entry)
    addresses = [entry["address"] for entry in projected]
    if len(set(addresses)) != len(addresses):
        raise ValueError("resource_changes[] addresses must be unique")
    projected.sort(key=lambda entry: entry["address"])
    return projected


def _canonical_bytes(plan_json: Mapping[str, object]) -> bytes:
    # Platform 함수가 있으면 그대로 재사용해 규칙이 갈리지 않게 한다.
    try:
        from packages.contracts.terraform_plan import canonical_plan_bytes

        return canonical_plan_bytes(plan_json)
    except Exception:  # noqa: BLE001 - 고객 repo에는 패키지가 없을 수 있다.
        text = json.dumps(
            _project(plan_json),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
        return text.encode("utf-8")


def main(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: canonical_plan_hash.py <plan.full.json>\n")
        return 2
    with open(argv[1], encoding="utf-8") as handle:
        plan_json = json.load(handle)
    if not isinstance(plan_json, dict):
        sys.stderr.write("plan JSON must be an object\n")
        return 2
    sys.stdout.buffer.write(_canonical_bytes(plan_json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
