#!/usr/bin/env python3
"""Publish the committed MVP Rule Registry into one customer's DynamoDB catalog.

이 스크립트는 A 소유의 배포/운영 진입점이다. `DynamoDbPolicyCatalogBootstrap`은
조건부 write만 수행하므로 재실행해도 안전하고, 같은 key에 다른 내용이 이미 있으면
Assessment가 이미 사용한 정책을 바꾸지 않고 fail-closed한다.

이 경로는 고객 정책 업로드 기능이 아니다. 검토·커밋된 Registry만 게시하며, 고객 업로드
경로는 `docs/POLICY_INGESTION.md`의 별도 Delivery gate를 따른다.

Registry는 둘이다(`--registry`). `legacy`는 `fixtures/rules/`의 자동 평가 Rule 16개이고,
`isms-p-2023`은 `fixtures/baselines/isms-p-2023/`의 ISMS-P 인증기준 기준선이다 — 101개 항목
전부가 MANUAL Rule이며 `profile-isms-p-baseline@v1`로 게시된다(ADR-0026). 고객은 ISMS-P를
업로드하지 않는다. 게시된 기준선을 Profile 게시 때 `baseline`으로 고른다.

    python scripts/publish_policy_catalog.py --customer-id cust-001 \
        --table-name proj-sandbox-metadata --dry-run
    python scripts/publish_policy_catalog.py --customer-id cust-001 \
        --registry isms-p-2023 --table-name proj-sandbox-metadata --region us-east-1

`--dry-run`은 AWS 자격 증명 없이 Registry를 검증하고 게시 대상 수만 보고한다.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.backend.policy import (  # noqa: E402 - repository root must precede the import.
    DynamoDbPolicyCatalogBootstrap,
    PolicyCatalogBootstrapError,
    load_rule_registry,
)

#: 게시할 수 있는 Registry. 둘 다 `load_rule_registry`가 읽는 같은 파일 모양이며, 같은
#: bootstrap이 게시한다. 디렉터리를 나눈 이유는 legacy Registry가 "세 관점으로 평가되는 legacy
#: Rule만 담는다"는 계약을 갖고 있어서다(`fixtures/README.md`).
REGISTRIES = {
    "legacy": REPO_ROOT / "fixtures" / "rules",
    "isms-p-2023": REPO_ROOT / "fixtures" / "baselines" / "isms-p-2023",
}


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    registry = load_rule_registry(REGISTRIES[arguments.registry])
    # Profile마다 판본 이력과 current pointer 두 item이 써진다(`_items_for_registry`). 하나로
    # 세면 "already matched" 수가 1 모자라게 보고된다 — 실제로 그렇게 보고됐다.
    planned = len(registry.sources) + len(registry.rules) + 2 * len(registry.profiles)
    print(
        f"registry {arguments.registry}: {len(registry.sources)} sources, "
        f"{len(registry.rules)} rules, {len(registry.profiles)} profiles ({planned} items)"
    )
    if arguments.dry_run:
        print("dry run: no DynamoDB write was attempted")
        return 0
    try:
        import boto3
    except ImportError:
        print("boto3 is required to publish; install it or use --dry-run", file=sys.stderr)
        return 2
    table = boto3.resource("dynamodb", region_name=arguments.region).Table(arguments.table_name)
    bootstrap = DynamoDbPolicyCatalogBootstrap(table, customer_id=arguments.customer_id)
    try:
        written = bootstrap.publish(registry)
    except PolicyCatalogBootstrapError as error:
        print(f"publish failed: {error}", file=sys.stderr)
        return 1
    print(f"published {written} new items; {planned - written} already matched")
    return 0


def _parse_arguments(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--customer-id", required=True, help="Approved customer partition key")
    parser.add_argument(
        "--registry",
        choices=sorted(REGISTRIES),
        default="legacy",
        help="Which committed registry to publish (default: legacy)",
    )
    parser.add_argument("--table-name", help="Metadata DynamoDB table name")
    parser.add_argument("--region", default="us-east-1", help="Customer platform region")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the registry and report the plan without writing",
    )
    arguments = parser.parse_args(argv)
    if not arguments.dry_run and not arguments.table_name:
        parser.error("--table-name is required unless --dry-run is used")
    return arguments


if __name__ == "__main__":
    raise SystemExit(main())
