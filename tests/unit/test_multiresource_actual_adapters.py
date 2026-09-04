"""EC2/RDS/ALB Actual-state adapters stay read-only and scoped without AWS credentials."""

import unittest
from datetime import UTC, datetime, timedelta

from agent.runtime import (
    ACTUAL_READ_RESOURCE_TYPES,
    ALB_RESOURCE_TYPE,
    EC2_INSTANCE_RESOURCE_TYPE,
    RDS_INSTANCE_RESOURCE_TYPE,
    S3_RESOURCE_TYPE,
    AssumeRoleAlbResourceTool,
    AssumeRoleEc2ResourceTool,
    AssumeRoleRdsResourceTool,
    AssumeRoleS3ResourceTool,
    AwsResourceNotFoundError,
    AwsResourceScopeError,
    AwsResourceToolError,
    ResourceTypeRoutingAwsResourceTool,
    build_actual_resource_tool,
)
from packages.contracts import AwsResourceOperation, AwsResourceQuery

CUSTOMER = "cust-001"
ACCOUNT = "123456789012"
ROLE = "arn:aws:iam::123456789012:role/read"
EXTERNAL_ID = "random-customer-bound-external-id"
INSTANCE_ID = "i-0123456789abcdef0"
DB_IDENTIFIER = "demo-db-001"
ALB_ARN = (
    "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/demo/50dc6c495c0c9188"
)


class Sts:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "Credentials": {
                "AccessKeyId": "key",
                "SecretAccessKey": "secret",
                "SessionToken": "token",
                "Expiration": datetime.now(UTC) + timedelta(minutes=10),
            }
        }


class NotFound(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def query(resource_type, operation, resource_id=None):
    return AwsResourceQuery(
        customer_id=CUSTOMER,
        aws_account_id=ACCOUNT,
        operation=operation,
        resource_type=resource_type,
        resource_id=resource_id,
    )


class Ec2:
    def __init__(self, *, missing=False):
        self.missing = missing
        self.volume_ids = None
        self.group_ids = None

    def describe_instances(self, **kwargs):
        if self.missing:
            raise NotFound("InvalidInstanceID.NotFound")
        return {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": INSTANCE_ID,
                            "InstanceType": "t3.micro",
                            "State": {"Name": "running"},
                            "SubnetId": "subnet-1",
                            "PublicIpAddress": "203.0.113.10",
                            # Sensitive fields the adapter must not project.
                            "UserData": "c2VjcmV0",
                            "KeyName": "demo-key",
                            "Tags": [{"Key": "Owner", "Value": "someone@example.com"}],
                            "NetworkInterfaces": [
                                {
                                    "NetworkInterfaceId": "eni-1",
                                    "SubnetId": "subnet-1",
                                    "Association": {"PublicIp": "203.0.113.10"},
                                }
                            ],
                            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-1"}}],
                            "SecurityGroups": [{"GroupId": "sg-1"}],
                        }
                    ]
                }
            ]
        }

    def describe_volumes(self, **kwargs):
        self.volume_ids = kwargs.get("VolumeIds")
        return {"Volumes": [{"VolumeId": "vol-1", "Encrypted": False, "Size": 8}]}

    def describe_security_groups(self, **kwargs):
        self.group_ids = kwargs.get("GroupIds")
        return {
            "SecurityGroups": [
                {
                    "GroupId": "sg-1",
                    "GroupName": "demo",
                    "IpPermissions": [
                        {
                            "IpProtocol": "tcp",
                            "FromPort": 22,
                            "ToPort": 22,
                            "IpRanges": [{"CidrIp": "0.0.0.0/0"}],
                        }
                    ],
                }
            ]
        }


class Ec2AdapterTest(unittest.TestCase):
    def _tool(self, client):
        return AssumeRoleEc2ResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            ec2_client_factory=lambda credentials: client,
        )

    def test_reads_instance_volume_and_security_group_state(self):
        client = Ec2()
        view = self._tool(client).read_resource(
            query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
        )

        self.assertEqual(view.resource_id, INSTANCE_ID)
        self.assertEqual(view.attributes["instance"]["PublicIpAddress"], "203.0.113.10")
        self.assertIs(view.attributes["volumes"][0]["Encrypted"], False)
        self.assertEqual(
            view.attributes["security_groups"][0]["IpPermissions"][0]["IpRanges"][0]["CidrIp"],
            "0.0.0.0/0",
        )
        # The instance's own volumes and groups are the only ones read.
        self.assertEqual(client.volume_ids, ["vol-1"])
        self.assertEqual(client.group_ids, ["sg-1"])

    def test_does_not_project_user_data_key_material_or_tags(self):
        """평가 근거가 아닌 필드는 evidence 문서에 들어가지 않는다."""
        view = self._tool(Ec2()).read_resource(
            query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
        )

        instance = view.attributes["instance"]
        for absent in ("UserData", "KeyName", "Tags"):
            self.assertNotIn(absent, instance)

    def test_lists_every_instance_as_a_full_view(self):
        views = self._tool(Ec2()).list_resources(
            query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual([view.resource_id for view in views], [INSTANCE_ID])

    def test_reports_a_missing_instance_as_not_found(self):
        with self.assertRaises(AwsResourceNotFoundError):
            self._tool(Ec2(missing=True)).read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
            )

    def test_rejects_a_resource_type_the_adapter_does_not_own(self):
        with self.assertRaisesRegex(AwsResourceToolError, "AWS::EC2::Instance"):
            self._tool(Ec2()).read_resource(
                query(S3_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, "bucket")
            )

    def test_rejects_a_query_outside_the_tool_scope(self):
        other = AwsResourceQuery(
            customer_id="cust-999",
            aws_account_id=ACCOUNT,
            operation=AwsResourceOperation.READ_RESOURCE,
            resource_type=EC2_INSTANCE_RESOURCE_TYPE,
            resource_id=INSTANCE_ID,
        )
        with self.assertRaises(AwsResourceScopeError):
            self._tool(Ec2()).read_resource(other)

    def test_rejects_a_write_shaped_operation(self):
        with self.assertRaisesRegex(AwsResourceToolError, "operation must be"):
            self._tool(Ec2()).read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
            )


class Rds:
    def __init__(self, *, missing=False):
        self.missing = missing

    def describe_db_instances(self, **kwargs):
        if self.missing:
            raise NotFound("DBInstanceNotFound")
        return {
            "DBInstances": [
                {
                    "DBInstanceIdentifier": DB_IDENTIFIER,
                    "Engine": "postgres",
                    "PubliclyAccessible": True,
                    "StorageEncrypted": False,
                    "IAMDatabaseAuthenticationEnabled": False,
                    "EnabledCloudwatchLogsExports": ["postgresql"],
                    "MasterUsername": "admin",
                    "Endpoint": {"Address": "demo.example.com", "Port": 5432},
                    "DBSubnetGroup": {"DBSubnetGroupName": "demo-subnets", "VpcId": "vpc-1"},
                    "VpcSecurityGroups": [
                        {"VpcSecurityGroupId": "sg-1", "Status": "active"},
                    ],
                }
            ]
        }


class RdsAdapterTest(unittest.TestCase):
    def _tool(self, client, ec2=None):
        return AssumeRoleRdsResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            rds_client_factory=lambda credentials: client,
            ec2_client_factory=lambda credentials: ec2 or Ec2(),
        )

    def test_reads_the_state_the_four_rds_rules_cite(self):
        view = self._tool(Rds()).read_resource(
            query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
        )

        instance = view.attributes["db_instance"]
        self.assertIs(instance["PubliclyAccessible"], True)
        self.assertIs(instance["StorageEncrypted"], False)
        self.assertEqual(instance["EnabledCloudwatchLogsExports"], ("postgresql",))
        self.assertEqual(view.attributes["db_subnet_group"]["DBSubnetGroupName"], "demo-subnets")
        self.assertEqual(view.attributes["vpc_security_groups"][0]["VpcSecurityGroupId"], "sg-1")
        self.assertEqual(
            view.attributes["vpc_security_groups"][0]["IpPermissions"][0]["IpRanges"][0]["CidrIp"],
            "0.0.0.0/0",
        )

    def test_does_not_project_connection_credentials(self):
        view = self._tool(Rds()).read_resource(
            query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
        )

        for absent in ("MasterUsername", "Endpoint"):
            self.assertNotIn(absent, view.attributes["db_instance"])

    def test_lists_db_instances(self):
        views = self._tool(Rds()).list_resources(
            query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual([view.resource_id for view in views], [DB_IDENTIFIER])

    def test_reports_a_missing_db_instance_as_not_found(self):
        with self.assertRaises(AwsResourceNotFoundError):
            self._tool(Rds(missing=True)).read_resource(
                query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
            )

    def test_refuses_a_partial_security_group_read(self):
        class MissingSecurityGroup(Ec2):
            def describe_security_groups(self, **kwargs):
                return {"SecurityGroups": []}

        with self.assertRaisesRegex(AwsResourceToolError, "fewer groups"):
            self._tool(Rds(), MissingSecurityGroup()).read_resource(
                query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
            )

    def test_rejects_a_resource_type_the_adapter_does_not_own(self):
        with self.assertRaisesRegex(AwsResourceToolError, "AWS::RDS::DBInstance"):
            self._tool(Rds()).read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
            )


class ElbV2:
    def __init__(self, *, missing=False, load_balancer_type="application"):
        self.missing = missing
        self.load_balancer_type = load_balancer_type

    def describe_load_balancers(self, **kwargs):
        if self.missing:
            raise NotFound("LoadBalancerNotFound")
        return {
            "LoadBalancers": [
                {
                    "LoadBalancerArn": ALB_ARN,
                    "LoadBalancerName": "demo",
                    "Type": self.load_balancer_type,
                    "Scheme": "internet-facing",
                    "VpcId": "vpc-1",
                    "SecurityGroups": ["sg-1"],
                }
            ]
        }

    def describe_listeners(self, **kwargs):
        return {
            "Listeners": [
                {"ListenerArn": "listener-1", "Port": 80, "Protocol": "HTTP"},
                {
                    "ListenerArn": "listener-2",
                    "Port": 443,
                    "Protocol": "HTTPS",
                    "SslPolicy": "ELBSecurityPolicy-TLS13-1-2-2021-06",
                },
            ]
        }

    def describe_load_balancer_attributes(self, **kwargs):
        return {
            "Attributes": [
                {"Key": "access_logs.s3.enabled", "Value": "false"},
                {"Key": "idle_timeout.timeout_seconds", "Value": "60"},
            ]
        }


class AlbAdapterTest(unittest.TestCase):
    def _tool(self, client):
        return AssumeRoleAlbResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            elbv2_client_factory=lambda credentials: client,
        )

    def test_reads_listeners_and_the_cited_attributes(self):
        view = self._tool(ElbV2()).read_resource(
            query(ALB_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, ALB_ARN)
        )

        self.assertEqual(view.resource_id, ALB_ARN)
        self.assertEqual(
            [listener["Protocol"] for listener in view.attributes["listeners"]],
            ["HTTP", "HTTPS"],
        )
        self.assertEqual(
            view.attributes["load_balancer_attributes"]["access_logs.s3.enabled"], "false"
        )
        # Attribute keys outside the cited list are dropped.
        self.assertNotIn(
            "idle_timeout.timeout_seconds", view.attributes["load_balancer_attributes"]
        )

    def test_rejects_reading_a_non_application_load_balancer(self):
        with self.assertRaisesRegex(AwsResourceToolError, "application load balancer"):
            self._tool(ElbV2(load_balancer_type="network")).read_resource(
                query(ALB_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, ALB_ARN)
            )

    def test_skips_non_application_load_balancers_when_listing(self):
        views = self._tool(ElbV2(load_balancer_type="network")).list_resources(
            query(ALB_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual(views, ())

    def test_lists_application_load_balancers_by_arn(self):
        views = self._tool(ElbV2()).list_resources(
            query(ALB_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual([view.resource_id for view in views], [ALB_ARN])

    def test_reports_a_missing_load_balancer_as_not_found(self):
        with self.assertRaises(AwsResourceNotFoundError):
            self._tool(ElbV2(missing=True)).read_resource(
                query(ALB_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, ALB_ARN)
            )


class ResourceTypeRoutingTest(unittest.TestCase):
    def _router(self):
        return ResourceTypeRoutingAwsResourceTool(
            {
                EC2_INSTANCE_RESOURCE_TYPE: AssumeRoleEc2ResourceTool(
                    customer_id=CUSTOMER,
                    aws_account_id=ACCOUNT,
                    role_arn=ROLE,
                    external_id=EXTERNAL_ID,
                    sts=Sts(),
                    ec2_client_factory=lambda credentials: Ec2(),
                ),
                RDS_INSTANCE_RESOURCE_TYPE: AssumeRoleRdsResourceTool(
                    customer_id=CUSTOMER,
                    aws_account_id=ACCOUNT,
                    role_arn=ROLE,
                    external_id=EXTERNAL_ID,
                    sts=Sts(),
                    rds_client_factory=lambda credentials: Rds(),
                    ec2_client_factory=lambda credentials: Ec2(),
                ),
            }
        )

    def test_routes_each_type_to_the_adapter_that_owns_it(self):
        router = self._router()

        self.assertEqual(
            router.read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
            ).resource_id,
            INSTANCE_ID,
        )
        self.assertEqual(
            router.read_resource(
                query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
            ).resource_id,
            DB_IDENTIFIER,
        )

    def test_an_unregistered_type_fails_closed_instead_of_returning_nothing(self):
        """미배선 type이 빈 결과로 통과하면 '위반 없음'과 구별되지 않는다."""
        with self.assertRaisesRegex(AwsResourceToolError, "no read adapter is configured"):
            self._router().read_resource(
                query(ALB_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, ALB_ARN)
            )

    def test_reports_the_types_this_deployment_can_read(self):
        self.assertEqual(
            self._router().resource_types,
            (EC2_INSTANCE_RESOURCE_TYPE, RDS_INSTANCE_RESOURCE_TYPE),
        )
        self.assertTrue(self._router().supports(EC2_INSTANCE_RESOURCE_TYPE))
        self.assertFalse(self._router().supports(ALB_RESOURCE_TYPE))
        self.assertFalse(self._router().supports(None))

    def test_rejects_an_empty_or_malformed_adapter_map(self):
        with self.assertRaisesRegex(ValueError, "non-empty mapping"):
            ResourceTypeRoutingAwsResourceTool({})
        with self.assertRaisesRegex(TypeError, "must implement AwsResourceTool"):
            ResourceTypeRoutingAwsResourceTool({EC2_INSTANCE_RESOURCE_TYPE: object()})

    def test_rds_factory_wires_both_rds_and_ec2_read_clients(self) -> None:
        services = []

        def provider(service):
            services.append(service)
            return lambda credentials: object()

        tool = build_actual_resource_tool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            resource_types=(RDS_INSTANCE_RESOURCE_TYPE,),
            client_factory_provider=provider,
            sts=Sts(),
        )

        self.assertTrue(tool.supports(RDS_INSTANCE_RESOURCE_TYPE))
        self.assertEqual(services, ["rds", "ec2"])


class PaginatedListTest(unittest.TestCase):
    """잘린 목록은 "위반 없음"과 구별되지 않는다. 모든 목록 조회는 토큰을 따라간다."""

    def test_ec2_list_follows_the_next_token(self) -> None:
        class PagedEc2(Ec2):
            def __init__(self):
                super().__init__()
                self.tokens = []

            def describe_instances(self, **kwargs):
                if "InstanceIds" in kwargs:
                    # A single-instance read is not paginated; answer the requested id.
                    requested = kwargs["InstanceIds"][0]
                    response = super().describe_instances()
                    response["Reservations"][0]["Instances"][0]["InstanceId"] = requested
                    return response
                self.tokens.append(kwargs.get("NextToken"))
                if kwargs.get("NextToken") is None:
                    return {
                        "Reservations": [{"Instances": [{"InstanceId": "i-first"}]}],
                        "NextToken": "page-2",
                    }
                return super().describe_instances()

        client = PagedEc2()
        views = self._tool(AssumeRoleEc2ResourceTool, "ec2_client_factory", client).list_resources(
            query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual([view.resource_id for view in views], ["i-first", INSTANCE_ID])
        self.assertIn("page-2", client.tokens)

    def test_rds_list_follows_the_marker(self) -> None:
        class PagedRds(Rds):
            def describe_db_instances(self, **kwargs):
                if kwargs.get("Marker") is None and "DBInstanceIdentifier" not in kwargs:
                    return {
                        "DBInstances": [{"DBInstanceIdentifier": "first-db"}],
                        "Marker": "page-2",
                    }
                return super().describe_db_instances(**kwargs)

        views = self._tool(
            AssumeRoleRdsResourceTool, "rds_client_factory", PagedRds()
        ).list_resources(query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES))

        self.assertEqual([view.resource_id for view in views], ["first-db", DB_IDENTIFIER])

    def test_alb_listeners_follow_the_marker(self) -> None:
        """리스너가 잘리면 평문 HTTP 리스너가 보이지 않아 ALB-HTTPS-001이 잘못 PASS한다."""

        class PagedElbV2(ElbV2):
            def describe_listeners(self, **kwargs):
                if kwargs.get("Marker") is None:
                    return {
                        "Listeners": [{"ListenerArn": "listener-0", "Protocol": "HTTP"}],
                        "Marker": "page-2",
                    }
                return super().describe_listeners(**kwargs)

        view = self._tool(
            AssumeRoleAlbResourceTool, "elbv2_client_factory", PagedElbV2()
        ).read_resource(query(ALB_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, ALB_ARN))

        self.assertEqual(
            [listener["Protocol"] for listener in view.attributes["listeners"]],
            ["HTTP", "HTTP", "HTTPS"],
        )

    def test_a_list_that_never_terminates_fails_instead_of_looping(self) -> None:
        class EndlessRds(Rds):
            def describe_db_instances(self, **kwargs):
                return {"DBInstances": [{"DBInstanceIdentifier": "db"}], "Marker": "always"}

        with self.assertRaisesRegex(AwsResourceToolError, "maximum number of pages"):
            self._tool(
                AssumeRoleRdsResourceTool, "rds_client_factory", EndlessRds()
            ).list_resources(query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES))

    def test_s3_list_follows_the_continuation_token(self) -> None:
        """S3 `ListBuckets`도 페이지네이션한다. 확장 전 어댑터에 남아 있던 같은 결함이다."""

        class PagedS3:
            def __init__(self):
                self.tokens = []

            def list_buckets(self, **kwargs):
                self.tokens.append(kwargs.get("ContinuationToken"))
                if kwargs.get("ContinuationToken") is None:
                    return {"Buckets": [{"Name": "first-bucket"}], "ContinuationToken": "page-2"}
                return {"Buckets": [{"Name": "second-bucket"}]}

            def get_public_access_block(self, **kwargs):
                return {"PublicAccessBlockConfiguration": {"BlockPublicAcls": True}}

            def get_bucket_encryption(self, **kwargs):
                return {"ServerSideEncryptionConfiguration": {}}

            def get_bucket_policy_status(self, **kwargs):
                return {"PolicyStatus": {}}

        client = PagedS3()
        views = self._tool(AssumeRoleS3ResourceTool, "s3_client_factory", client).list_resources(
            query(S3_RESOURCE_TYPE, AwsResourceOperation.LIST_RESOURCES)
        )

        self.assertEqual([view.resource_id for view in views], ["first-bucket", "second-bucket"])
        self.assertEqual(client.tokens, [None, "page-2"])

    @staticmethod
    def _tool(adapter, factory_name, client):
        factories = {factory_name: lambda credentials: client}
        if adapter is AssumeRoleRdsResourceTool:
            # RDS-ACCESS-001의 근거는 연결된 VPC 보안 그룹의 실제 ingress이므로 RDS adapter는
            # EC2 client도 요구한다. 이 테스트의 DB 인스턴스는 보안 그룹이 없어 호출되지 않는다.
            factories.setdefault("ec2_client_factory", lambda credentials: Ec2())
        return adapter(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            **factories,
        )


class IncompleteEvidenceTest(unittest.TestCase):
    """부분 응답이 준수 근거로 읽히면 안 된다."""

    def _tool(self, client):
        return AssumeRoleEc2ResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            ec2_client_factory=lambda credentials: client,
        )

    def test_a_missing_volume_fails_instead_of_reading_as_encrypted(self) -> None:
        class PartialEc2(Ec2):
            def describe_instances(self, **kwargs):
                response = super().describe_instances(**kwargs)
                instance = response["Reservations"][0]["Instances"][0]
                instance["BlockDeviceMappings"] = [
                    {"Ebs": {"VolumeId": "vol-1"}},
                    {"Ebs": {"VolumeId": "vol-2"}},
                ]
                return response

            def describe_volumes(self, **kwargs):
                # Only the encrypted volume comes back; `vol-2` is silently absent.
                return {"Volumes": [{"VolumeId": "vol-1", "Encrypted": True}]}

        with self.assertRaisesRegex(AwsResourceToolError, "fewer volumes"):
            self._tool(PartialEc2()).read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
            )

    def test_a_missing_security_group_fails_instead_of_reading_as_restricted(self) -> None:
        class PartialEc2(Ec2):
            def describe_instances(self, **kwargs):
                response = super().describe_instances(**kwargs)
                response["Reservations"][0]["Instances"][0]["SecurityGroups"] = [
                    {"GroupId": "sg-1"},
                    {"GroupId": "sg-2"},
                ]
                return response

            def describe_security_groups(self, **kwargs):
                return {"SecurityGroups": [{"GroupId": "sg-1", "IpPermissions": []}]}

        with self.assertRaisesRegex(AwsResourceToolError, "fewer groups"):
            self._tool(PartialEc2()).read_resource(
                query(EC2_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, INSTANCE_ID)
            )


class ProjectedFieldBoundaryTest(unittest.TestCase):
    """근거 문서는 Rule이 인용하는 필드만 담는다.

    두 방향 모두 위험하다. 필드가 빠지면 평가가 근거 없이 판단하고, 필드가 남으면 (1) 고객 내용이
    모델 입력과 저장 evidence로 흘러가며 (2) 어떤 Rule도 묻지 않은 상태를 평가기가 판단에 반영할
    수 있다. 그래서 허용 목록을 **정확히** 고정한다.
    """

    def test_ec2_projects_exactly_the_cited_fields(self) -> None:
        from agent.runtime import assume_role_ec2_resource_tool as ec2_adapter

        self.assertEqual(
            ec2_adapter._INSTANCE_FIELDS,
            ("InstanceId", "State", "SubnetId", "VpcId", "PublicIpAddress", "PublicDnsName"),
        )
        self.assertEqual(ec2_adapter._VOLUME_FIELDS, ("VolumeId", "Encrypted", "KmsKeyId"))
        self.assertEqual(
            ec2_adapter._SECURITY_GROUP_FIELDS,
            ("GroupId", "GroupName", "VpcId", "IpPermissions"),
        )
        # 인바운드 Rule에 대한 근거이므로 egress는 담지 않는다.
        self.assertNotIn("IpPermissionsEgress", ec2_adapter._SECURITY_GROUP_FIELDS)

    def test_rds_projects_exactly_the_cited_fields(self) -> None:
        from agent.runtime import assume_role_rds_resource_tool as rds_adapter

        self.assertEqual(
            rds_adapter._DB_INSTANCE_FIELDS,
            (
                "DBInstanceIdentifier",
                "DBInstanceStatus",
                "Engine",
                "PubliclyAccessible",
                "StorageEncrypted",
                "KmsKeyId",
                "IAMDatabaseAuthenticationEnabled",
                "EnabledCloudwatchLogsExports",
            ),
        )
        self.assertEqual(
            rds_adapter._SECURITY_GROUP_FIELDS,
            ("GroupId", "GroupName", "VpcId", "IpPermissions"),
        )
        self.assertNotIn("IpPermissionsEgress", rds_adapter._SECURITY_GROUP_FIELDS)

    def test_alb_projects_exactly_the_cited_fields(self) -> None:
        from agent.runtime import assume_role_alb_resource_tool as alb_adapter

        self.assertEqual(
            alb_adapter._ATTRIBUTE_KEYS,
            ("access_logs.s3.enabled", "access_logs.s3.bucket", "access_logs.s3.prefix"),
        )
        self.assertEqual(
            alb_adapter._LOAD_BALANCER_FIELDS,
            ("LoadBalancerArn", "LoadBalancerName", "Type", "Scheme", "State", "VpcId"),
        )

    def test_a_projection_drops_an_unlisted_field_even_when_aws_returns_it(self) -> None:
        view = AssumeRoleRdsResourceTool(
            customer_id=CUSTOMER,
            aws_account_id=ACCOUNT,
            role_arn=ROLE,
            external_id=EXTERNAL_ID,
            sts=Sts(),
            rds_client_factory=lambda credentials: Rds(),
            ec2_client_factory=lambda credentials: Ec2(),
        ).read_resource(
            query(RDS_INSTANCE_RESOURCE_TYPE, AwsResourceOperation.READ_RESOURCE, DB_IDENTIFIER)
        )

        self.assertEqual(
            set(view.attributes["db_instance"]) - set(_RDS_RESPONSE_KEYS),
            set(),
            "projection must never invent a field",
        )
        for absent in ("MasterUsername", "Endpoint"):
            self.assertNotIn(absent, view.attributes["db_instance"])


#: The keys the RDS fake returns, used to assert the projection is a subset of the response.
_RDS_RESPONSE_KEYS = (
    "DBInstanceIdentifier",
    "Engine",
    "PubliclyAccessible",
    "StorageEncrypted",
    "IAMDatabaseAuthenticationEnabled",
    "EnabledCloudwatchLogsExports",
    "MasterUsername",
    "Endpoint",
    "DBSubnetGroup",
    "VpcSecurityGroups",
)


class SingleVocabularyTest(unittest.TestCase):
    """읽을 수 있는 유형 목록은 한 곳(어댑터 registry)이 정한다."""

    def test_every_consumer_reads_the_adapter_registry(self) -> None:
        from apps.backend.assessment.actual import SUPPORTED_ACTUAL_RESOURCE_TYPES
        from scripts.validate_m1_deployment_config import SUPPORTED_RESOURCE_TYPES

        self.assertEqual(SUPPORTED_ACTUAL_RESOURCE_TYPES, ACTUAL_READ_RESOURCE_TYPES)
        self.assertEqual(SUPPORTED_RESOURCE_TYPES, frozenset(ACTUAL_READ_RESOURCE_TYPES))

    def test_the_registry_covers_the_four_expanded_types(self) -> None:
        self.assertEqual(
            ACTUAL_READ_RESOURCE_TYPES,
            (
                S3_RESOURCE_TYPE,
                EC2_INSTANCE_RESOURCE_TYPE,
                RDS_INSTANCE_RESOURCE_TYPE,
                ALB_RESOURCE_TYPE,
            ),
        )

    def test_every_readable_type_maps_to_an_aws_service(self) -> None:
        from agent.runtime import aws_service_for

        self.assertEqual(
            [aws_service_for(resource_type) for resource_type in ACTUAL_READ_RESOURCE_TYPES],
            ["s3", "ec2", "rds", "elbv2"],
        )
        with self.assertRaisesRegex(AwsResourceToolError, "no Actual read adapter exists"):
            aws_service_for("AWS::DynamoDB::Table")


if __name__ == "__main__":
    unittest.main()
