# ADR-0008: Customer deployment topology

## Context

플랫폼은 고객 정책·IaC·AWS 상태를 다루므로 고객 Account 안에서 책임과 데이터 경계를 유지해야 한다. MVP에서 기존 고객 VPC 연결까지 지원하면 네트워크·운영 복잡도가 급격히 증가한다.

## Decision

MVP는 고객 AWS Account의 `us-east-1`에 Customer-Deployed 형태로 배포한다. Frontend는 S3 + CloudFront, 인증은 Cognito, API는 API Gateway와 기능별 Lambda, 상태는 DynamoDB/S3, AI Runtime은 Bedrock/Agent Runtime을 사용한다. Backend Lambda와 Agent Runtime은 MVP에서 고객 기존 VPC에 연결하지 않는다.

## Consequences

설치는 CloudFormation Packaging으로 제공하고, 고객별 IAM·데이터·감사 경계를 강제한다. 최초 GitHub Actions OIDC trust는 순환 의존을 피하기 위해 고객 관리자가 제공된 bootstrap stack으로 생성한다. bootstrap은 좁게 제한된 deployment role, versioned Lambda-code bucket, foundation 전용 CloudFormation execution role만 만들며, 이후 repository workflow가 그 role을 통해 foundation을 배포한다. Private VPC 연결, 다중 리전, 네트워크 격리는 확장 요구가 확인될 때 별도 ADR로 검토한다.
