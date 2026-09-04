"""Code-only, read-only Application Load Balancer implementation of the Resource Tool port.

The resource id is the load balancer ARN, not its name. A listener can only name its parent
by `load_balancer_arn`, so the ARN is the one identifier that keeps the load balancer and its
listeners in a single vocabulary — the same reason the Terraform plan projection reads
`aws_lb.arn` and `aws_lb_listener.load_balancer_arn`.

`ALB-HTTPS-001` is about listener protocol and TLS policy and `ALB-LOGGING-001` is about the
`access_logs.s3.enabled` attribute, so a load balancer view needs three reads: the load
balancer, its listeners, and its attributes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from time import time
from typing import Protocol

from agent.runtime.assume_role_session import (
    AssumeRoleReadSession,
    error_code,
    paginate,
    projected,
)
from agent.runtime.aws_resource_tool import (
    AwsResourceNotFoundError,
    AwsResourceTool,
    AwsResourceToolError,
    AwsResourceView,
    require_read_operation,
    require_scope,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

ALB_RESOURCE_TYPE = "AWS::ElasticLoadBalancingV2::LoadBalancer"

_NOT_FOUND_CODES = frozenset({"LoadBalancerNotFound", "LoadBalancerNotFoundException"})

#: Only `application` load balancers are in scope; a network load balancer has no HTTPS
#: listener concept and would be judged against the wrong Rules.
_APPLICATION_TYPE = "application"

_LOAD_BALANCER_FIELDS = (
    "LoadBalancerArn",
    "LoadBalancerName",
    "Type",
    "Scheme",
    "State",
    "VpcId",
)
_LISTENER_FIELDS = ("ListenerArn", "Port", "Protocol", "SslPolicy", "Certificates")
#: The attribute keys the logging Rule cites, and only those. Everything else
#: `describe_load_balancer_attributes` returns (idle timeout, deletion protection, header
#: handling) is state no ALB Rule asks about.
_ATTRIBUTE_KEYS = (
    "access_logs.s3.enabled",
    "access_logs.s3.bucket",
    "access_logs.s3.prefix",
)


class ElbV2Client(Protocol):
    def describe_load_balancers(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_listeners(self, **kwargs: object) -> Mapping[str, object]: ...

    def describe_load_balancer_attributes(self, **kwargs: object) -> Mapping[str, object]: ...


class AssumeRoleAlbResourceTool(AwsResourceTool):
    """Read ALB state through one approved Role ARN; no mutation API exists."""

    def __init__(
        self,
        *,
        customer_id: str,
        aws_account_id: str,
        role_arn: str,
        external_id: str,
        sts: object,
        elbv2_client_factory: Callable[[Mapping[str, str]], ElbV2Client],
        clock: Callable[[], float] = time,
    ) -> None:
        for name, value in (("customer_id", customer_id), ("aws_account_id", aws_account_id)):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if not callable(elbv2_client_factory):
            raise TypeError("elbv2_client_factory is required")
        self._customer_id, self._aws_account_id = customer_id, aws_account_id
        self._elbv2_client_factory = elbv2_client_factory
        self._session = AssumeRoleReadSession(
            role_arn=role_arn, external_id=external_id, sts=sts, clock=clock
        )

    def read_resource(self, query: AwsResourceQuery) -> AwsResourceView:
        query = require_read_operation(query, AwsResourceOperation.READ_RESOURCE)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_alb(query)
        arn = query.resource_id or ""
        client = self._elbv2()
        try:
            response = client.describe_load_balancers(LoadBalancerArns=[arn])
        except Exception as error:
            if error_code(error) in _NOT_FOUND_CODES:
                raise AwsResourceNotFoundError("load balancer state was not found") from None
            raise AwsResourceToolError("load balancer read failed") from None
        for balancer in _sequence(response.get("LoadBalancers")):
            if balancer.get("LoadBalancerArn") != arn:
                continue
            _require_application_type(balancer)
            return self._view(client, arn, balancer)
        raise AwsResourceNotFoundError("load balancer state was not found")

    def list_resources(self, query: AwsResourceQuery) -> Sequence[AwsResourceView]:
        query = require_read_operation(query, AwsResourceOperation.LIST_RESOURCES)
        require_scope(query, customer_id=self._customer_id, aws_account_id=self._aws_account_id)
        _require_alb(query)
        client = self._elbv2()
        try:
            balancers = paginate(
                client.describe_load_balancers,
                items_key="LoadBalancers",
                token_argument="Marker",
            )
        except AwsResourceToolError:
            raise
        except Exception:
            raise AwsResourceToolError("load balancer list failed") from None
        views = []
        for balancer in balancers:
            arn = balancer.get("LoadBalancerArn")
            if not isinstance(arn, str) or not arn:
                raise AwsResourceToolError("load balancer list is invalid")
            # A non-application load balancer is skipped, not rejected: the account may
            # legitimately run network load balancers that these Rules do not describe.
            if balancer.get("Type") != _APPLICATION_TYPE:
                continue
            views.append(self._view(client, arn, balancer))
        return tuple(views)

    def _view(
        self, client: ElbV2Client, arn: str, balancer: Mapping[str, object]
    ) -> AwsResourceView:
        """Compose the load balancer, its listeners, and the cited attributes into one view.

        Unlike the EC2 adapter there is no completeness cross-check available here. EC2 knows
        the expected set independently — the instance itself names its volumes and security
        groups — so a short response can be detected. A load balancer does not declare how
        many listeners it has; `describe_listeners` *is* the only statement of that. The
        pagination guard is therefore the whole defence: it is what stops a listener set from
        being silently cut short. An honestly empty listener set is left as-is rather than
        rejected, because a load balancer with no listeners is a legitimate state and
        `ALB-HTTPS-001` (which requires an HTTPS listener) already fails closed on it.
        """
        try:
            listeners = paginate(
                client.describe_listeners,
                items_key="Listeners",
                token_argument="Marker",
                request={"LoadBalancerArn": arn},
            )
            attributes = client.describe_load_balancer_attributes(LoadBalancerArn=arn).get(
                "Attributes", []
            )
        except AwsResourceToolError:
            raise
        except Exception:
            raise AwsResourceToolError("load balancer read failed") from None
        return AwsResourceView(
            aws_account_id=self._aws_account_id,
            resource_type=ALB_RESOURCE_TYPE,
            resource_id=arn,
            attributes={
                "load_balancer": projected(balancer, _LOAD_BALANCER_FIELDS),
                "listeners": [projected(listener, _LISTENER_FIELDS) for listener in listeners],
                # `attributes.attributes`라는 중첩 이름은 모델을 오도했다: 라이브 A/B(3회씩)에서
                # HTTPS 리스너가 있는 ALB를 `access_logs.s3.enabled=false`만 보고 FAIL로 판정했고,
                # 이 key를 `load_balancer_attributes`로 바꾸자 같은 문서가 3/3 PASS였다.
                "load_balancer_attributes": _selected_attributes(attributes),
            },
        )

    def _elbv2(self) -> ElbV2Client:
        return self._elbv2_client_factory(self._session.credentials())


def _selected_attributes(attributes: object) -> dict[str, object]:
    """Flatten the `[{Key, Value}]` attribute list down to the cited keys.

    A key whose `Value` is absent is left out rather than stored as `None`: "the attribute
    was not reported" and "the attribute is set to nothing" are different states, and only
    the response can say which one this is.
    """
    selected: dict[str, object] = {}
    for entry in _sequence(attributes):
        key, value = entry.get("Key"), entry.get("Value")
        if isinstance(key, str) and key in _ATTRIBUTE_KEYS and isinstance(value, str):
            selected[key] = value
    return selected


def _require_alb(query: AwsResourceQuery) -> None:
    if query.resource_type != ALB_RESOURCE_TYPE:
        raise AwsResourceToolError(
            "ALB adapter supports only AWS::ElasticLoadBalancingV2::LoadBalancer"
        )


def _require_application_type(balancer: Mapping[str, object]) -> None:
    if balancer.get("Type") != _APPLICATION_TYPE:
        raise AwsResourceToolError("load balancer is not an application load balancer")


def _sequence(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]
