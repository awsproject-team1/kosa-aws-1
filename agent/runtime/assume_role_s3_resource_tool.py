"""Code-only, read-only S3 implementation of the AWS Resource Tool port."""

from __future__ import annotations

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
        try:
            policy = s3.get_bucket_policy_status(Bucket=bucket).get("PolicyStatus", {})
        except Exception as error:
            if error_code(error) == "NoSuchBucketPolicy":
                policy = {}
            elif error_code(error) == "NoSuchBucket":
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
