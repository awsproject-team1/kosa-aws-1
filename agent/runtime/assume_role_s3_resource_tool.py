"""Code-only, read-only S3 implementation of the AWS Resource Tool port."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from time import time
from typing import Protocol

from agent.runtime.aws_resource_tool import (
    AwsResourceNotFoundError,
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
    require_read_operation,
    require_scope,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery


class StsClient(Protocol):
    def assume_role(self, **kwargs: object) -> Mapping[str, object]: ...


class S3Client(Protocol):
    def list_buckets(self) -> Mapping[str, object]: ...

    def get_public_access_block(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_encryption(self, **kwargs: object) -> Mapping[str, object]: ...

    def get_bucket_policy_status(self, **kwargs: object) -> Mapping[str, object]: ...


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
            ("role_arn", role_arn),
            ("external_id", external_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if sts is None or not callable(s3_client_factory) or not callable(clock):
            raise TypeError("sts, s3_client_factory, and clock are required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._role_arn, self._sts, self._s3_client_factory = role_arn, sts, s3_client_factory
        self._external_id, self._clock = external_id, clock
        self._cached_credentials: Mapping[str, str] | None = None
        self._credentials_expire_at: float | None = None

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
            if _code(error) == "NoSuchBucket":
                raise AwsResourceNotFoundError("S3 bucket state was not found") from None
            raise AwsResourceToolError("S3 bucket read failed") from None
        try:
            policy = s3.get_bucket_policy_status(Bucket=bucket).get("PolicyStatus", {})
        except Exception as error:
            if _code(error) == "NoSuchBucketPolicy":
                policy = {}
            elif _code(error) == "NoSuchBucket":
                raise AwsResourceNotFoundError("S3 bucket state was not found") from None
            else:
                raise AwsResourceToolError("S3 bucket read failed") from None
        return AwsResourceView(
            aws_account_id=self._aws_account_id,
            resource_type=query.resource_type,
            resource_id=bucket,
            attributes={"public_access_block": block, "encryption": encryption, "policy": policy},
        )

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        self._require_s3(query)
        try:
            buckets = self._s3().list_buckets().get("Buckets", [])
        except Exception:
            raise AwsResourceToolError("S3 bucket list failed") from None
        if not isinstance(buckets, list):
            raise AwsResourceToolError("S3 bucket list is invalid")
        views = []
        for bucket in buckets:
            name = bucket.get("Name") if isinstance(bucket, Mapping) else None
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
        try:
            if self._cached_credentials is None or not self._credentials_are_valid():
                response = self._sts.assume_role(
                    RoleArn=self._role_arn,
                    RoleSessionName="governance-read",
                    ExternalId=self._external_id,
                )
                values = response.get("Credentials")
                if not isinstance(values, Mapping):
                    raise ValueError
                required = {
                    name: values[name]
                    for name in ("AccessKeyId", "SecretAccessKey", "SessionToken")
                }
                if not all(isinstance(value, str) and value for value in required.values()):
                    raise ValueError
                self._cached_credentials = required
                self._credentials_expire_at = _expiration_epoch(values.get("Expiration"))
            return self._s3_client_factory(self._cached_credentials)
        except Exception:
            raise AwsResourceToolError("AWS read role assumption failed") from None

    def _credentials_are_valid(self) -> bool:
        return (
            self._credentials_expire_at is not None
            and self._credentials_expire_at > self._clock() + 60
        )

    @staticmethod
    def _require_s3(query: AwsResourceQuery) -> None:
        if query.resource_type != "AWS::S3::Bucket":
            raise AwsResourceToolError("S3 adapter supports only AWS::S3::Bucket")


def _code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    details = response.get("Error") if isinstance(response, Mapping) else None
    value = details.get("Code") if isinstance(details, Mapping) else None
    return value if isinstance(value, str) else None


def _expiration_epoch(value: object) -> float | None:
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None
