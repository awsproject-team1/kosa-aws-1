"""Code-only, read-only S3 implementation of the AWS Resource Tool port."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from time import time
from typing import Protocol

from agent.runtime.assume_role_session import AssumeRoleReadSession, error_code, paginate
from agent.runtime.aws_resource_tool import (
    AwsResourceNotFoundError,
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
    require_read_operation,
    require_scope,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

S3_RESOURCE_TYPE = "AWS::S3::Bucket"


class StsClient(Protocol):
    def assume_role(self, **kwargs: object) -> Mapping[str, object]: ...


class S3Client(Protocol):
    def list_buckets(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_public_access_block(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_encryption(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_policy_status(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_policy(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_ownership_controls(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_logging(self, **kwargs: object) -> Mapping[str, object]: ...


#: 버킷에 OwnershipControls가 설정돼 있지 않을 때 S3가 실제로 적용하는 값. 추측이 아니라 S3의
#: 문서화된 기본 동작이다 — ACL이 켜져 있고 object writer가 소유한다.
_DEFAULT_OBJECT_OWNERSHIP = "ObjectWriter"

#: 정책 본문 크기 상한. S3 자체 상한(20 KB)보다 넉넉하며, 그 이상은 근거 문서가 아니라 payload다.
_MAX_POLICY_BYTES = 64 * 1024


class AssumeRoleS3ResourceTool(AwsResourceTool):
    """Read S3 state through one approved Role ARN; no mutation API exists."""

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        role_arn: str,
        external_id: str,
        sts: StsClient,
        s3_client_factory: Callable[[Mapping[str, str]], S3Client],
        clock: Callable[[], float] = time,
    ) -> None:
        for name, value in (
            ("customer_id", customer_id),
            ("aws_account_id", aws_account_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(s3_client_factory):
            raise TypeError("s3_client_factory is required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._s3_client_factory = s3_client_factory
        self._session = AssumeRoleReadSession(
            role_arn=role_arn, external_id=external_id, sts=sts, clock=clock
        )

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        query = require_read_operation(query, AwsResourceOperation.READ_RESOURCE)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        self._require_s3(query)
        bucket = query.resource_id or ""
        s3 = self._s3()
        try:
            block = s3.get_public_access_block(Bucket=bucket).get(
                "PublicAccessBlockConfiguration", {}
            )
            encryption = s3.get_bucket_encryption(Bucket=bucket).get(
                "ServerSideEncryptionConfiguration", {}
            )
        except Exception as error:
            if error_code(error) == "NoSuchBucket":
                raise AwsResourceNotFoundError("S3 bucket state was not found") from None
            raise AwsResourceToolError("S3 bucket read failed") from None
        policy_status = _optional_read(
            lambda: s3.get_bucket_policy_status(Bucket=bucket).get("PolicyStatus", {}),
            absent_codes=("NoSuchBucketPolicy",),
            absent_value={},
        )
        # 아래 셋은 2026-09-05에 추가됐다. 그 전에는 ACL·Bucket Policy·TLS·Logging Rule의 AWS
        # 좌표에 답이 존재할 수 없었고, 게이트가 없던 그 좌표에서 모델은 public-access-block
        # 플래그를 근거로 PASS를 냈다. "설정 없음"은 field 부재가 아니라 명시된 값으로 투영한다 —
        # 부재는 "읽지 못함"이고 없음은 사실이며, 둘을 같은 모양으로 두면 위반이 근거 부족이 된다.
        policy_document = _optional_read(
            lambda: s3.get_bucket_policy(Bucket=bucket).get("Policy"),
            absent_codes=("NoSuchBucketPolicy",),
            absent_value=None,
        )
        ownership = _optional_read(
            lambda: s3.get_bucket_ownership_controls(Bucket=bucket).get("OwnershipControls", {}),
            absent_codes=("OwnershipControlsNotFoundError",),
            absent_value=None,
        )
        logging = _optional_read(
            lambda: s3.get_bucket_logging(Bucket=bucket).get("LoggingEnabled"),
            absent_codes=(),
            absent_value=None,
        )
        return AwsResourceView(
            aws_account_id=self._aws_account_id,
            resource_type=query.resource_type,
            resource_id=bucket,
            attributes={
                "public_access_block": block,
                "encryption": encryption,
                "policy": policy_status,
                "bucket_policy": _bucket_policy(policy_document),
                "ownership_controls": _ownership_controls(ownership),
                "logging": _logging(logging),
            },
        )

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        self._require_s3(query)
        try:
            buckets = paginate(
                self._s3().list_buckets,
                items_key="Buckets",
                token_argument="ContinuationToken",
            )
        except AwsResourceToolError:
            raise
        except Exception:
            raise AwsResourceToolError("S3 bucket list failed") from None
        views = []
        for bucket in buckets:
            name = bucket.get("Name")
            if not isinstance(name, str) or not name:
                raise AwsResourceToolError("S3 bucket list is invalid")
            views.append(
                self.read_resource(
                    AwsResourceQuery(
                        customer_id=self._customer_id,
                        aws_account_id=self._aws_account_id,
                        operation=AwsResourceOperation.READ_RESOURCE,
                        resource_type=query.resource_type,
                        resource_id=name,
                    )
                )
            )
        return tuple(views)

    def _s3(self) -> S3Client:
        return self._s3_client_factory(self._session.credentials())

    @staticmethod
    def _require_s3(query: AwsResourceQuery) -> None:
        if query.resource_type != S3_RESOURCE_TYPE:
            raise AwsResourceToolError("S3 adapter supports only AWS::S3::Bucket")


def _optional_read(
    read: Callable[[], object], *, absent_codes: tuple[str, ...], absent_value: object
) -> object:
    """Perform one bucket sub-read whose absence is itself a fact, not a failure.

    `NoSuchBucketPolicy`처럼 "설정이 없다"는 오류는 `absent_value`로 돌아온다. 그 외의 오류(권한
    없음, 네트워크)는 read 실패다 — 없는 것을 "없음"으로 읽으면 권한 하나 빠진 계정이 전부 위반
    또는 전부 준수로 보인다.
    """
    try:
        return read()
    except Exception as error:
        code = error_code(error)
        if code in absent_codes:
            return absent_value
        if code == "NoSuchBucket":
            raise AwsResourceNotFoundError("S3 bucket state was not found") from None
        raise AwsResourceToolError("S3 bucket read failed") from None


def _bucket_policy(document: object) -> dict[str, object]:
    """`{present, document}` — the parsed policy, or an explicit "no policy"."""
    if document is None:
        return {"present": False, "document": None}
    if not isinstance(document, str):
        raise AwsResourceToolError("S3 bucket policy is not a string")
    if len(document.encode("utf-8")) > _MAX_POLICY_BYTES:
        raise AwsResourceToolError("S3 bucket policy exceeds the evidence size limit")
    try:
        parsed = json.loads(document)
    except json.JSONDecodeError:
        raise AwsResourceToolError("S3 bucket policy is not JSON") from None
    if not isinstance(parsed, dict):
        raise AwsResourceToolError("S3 bucket policy is not a JSON object")
    return {"present": True, "document": parsed}


def _ownership_controls(controls: object) -> dict[str, object]:
    """`{ObjectOwnership, configured}` — the configured value, or S3's documented default."""
    if controls is None:
        return {"ObjectOwnership": _DEFAULT_OBJECT_OWNERSHIP, "configured": False}
    rules = controls.get("Rules") if isinstance(controls, Mapping) else None
    ownership = None
    if isinstance(rules, list):
        for rule in rules:
            if isinstance(rule, Mapping) and isinstance(rule.get("ObjectOwnership"), str):
                ownership = rule["ObjectOwnership"]
                break
    if ownership is None:
        raise AwsResourceToolError("S3 ownership controls carry no ObjectOwnership rule")
    return {"ObjectOwnership": ownership, "configured": True}


def _logging(logging_enabled: object) -> dict[str, object]:
    """`{enabled, target_bucket, target_prefix}` — disabled logging is a value, not an absence."""
    if logging_enabled is None:
        return {"enabled": False, "target_bucket": None, "target_prefix": None}
    if not isinstance(logging_enabled, Mapping):
        raise AwsResourceToolError("S3 logging configuration is invalid")
    target = logging_enabled.get("TargetBucket")
    if not isinstance(target, str) or not target:
        raise AwsResourceToolError("S3 logging configuration names no target bucket")
    prefix = logging_enabled.get("TargetPrefix")
    return {
        "enabled": True,
        "target_bucket": target,
        "target_prefix": prefix if isinstance(prefix, str) else None,
    }
