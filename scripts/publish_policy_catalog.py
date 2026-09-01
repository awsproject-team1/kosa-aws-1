#!/usr/bin/env python3
"""Publish the committed MVP Rule Registry into one customer's DynamoDB catalog.

이 스크립트는 A 소유의 배포/운영 진입점이다. `DynamoDbPolicyCatalogBootstrap`은
조건부 write만 수행하므로 재실행해도 안전하고, 같은 key에 다른 내용이 이미 있으면
Assessment가 이미 사용한 정책을 바꾸지 않고 fail-closed한다.

이 경로는 고객 정책 업로드 기능이 아니다. 검토·커밋된 Registry(`fixtures/rules/`)만
게시하며, 고객 업로드 경로는 `docs/POLICY_INGESTION.md`의 별도 Delivery gate를 따른다.

    python scripts/publish_policy_catalog.py --customer-id cust-001 \
        --table-name proj-sandbox-metadata --dry-run
    python scripts/publish_policy_catalog.py --customer-id cust-001 \
        --table-name proj-sandbox-metadata --region us-east-1

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

REGISTRY_DIR = REPO_ROOT / "fixtures" / "rules"


def main(argv: list[str] | None = None) -> int:
    arguments = _parse_arguments(argv)
    registry = load_rule_registry(REGISTRY_DIR)
    planned = len(registry.sources) + len(registry.rules) + len(registry.profiles)
    print(
        f"registry: {len(registry.sources)} sources, {len(registry.rules)} rules, "
        f"{len(registry.profiles)} profiles ({planned} items)"
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
