# ADR-0011: IaC·Actual 상태·Drift 평가 관점 분리

## 맥락

Terraform 구성은 원하는 상태(desired state)이지만, 고객의 실제 AWS 상태는 콘솔 변경, 레거시
리소스, 실패한 배포 또는 관리되지 않는 리소스 때문에 달라질 수 있다. 두 입력을 하나의 불투명한
Initial Assessment 결과로 합치면 정책 위반 또는 구성 Drift가 가려질 수 있고, Remediation이
IaC, AWS 또는 양쪽 중 어디를 변경해야 하는지도 불명확해진다.

## 결정

Terraform으로 관리되는 리소스의 Initial Assessment는 `IAC`, `AWS_ACTUAL`, `DRIFT` 관점별로
분리된 `Resource × Rule` 결과를 생성한다. 각 결과는 독립된 Evidence Reference를 기록한다.
Drift 결과는 원하는 IaC와 관측된 AWS 상태가 일치하지 않음을 뜻할 뿐, AI나 플랫폼에 고객
워크로드를 직접 변경할 권한을 부여하지 않는다.

IaC가 안전하지 않으면 Remediation은 승인된 보안 desired state로 IaC를 변경한다. IaC는 이미
안전하고 AWS Actual만 Drift인 경우에는 기존 IaC commit이 동기화 대상이며 Patch를 만들지 않는다.
Deployment Readiness는 현재 AWS 상태를 기준으로 갱신된 Terraform Plan을 실행하고, Patch 또는
동기화 대상의 차단이나 수정을 요구할 수 있다. AWS Actual을 변경하는 주체는 승인된 GitHub
Actions OIDC Apply뿐이다. Post-Deploy Verification은 Actual Compliance와 Drift를 다시 평가한다.
Terraform 관리 밖에 있거나 안전한 IaC-to-AWS 매핑이 없는 리소스는 자동 Patch 대신
`MANUAL_REVIEW` 결과를 낸다.

## 결과

Assessment 소비자는 각 결과의 evaluation perspective와 evidence를 보존해야 한다. 구현이
확장되면서 Golden fixture는 각 perspective를 다룬다. M0에서는 별도 Drift entity를 도입하지
않고 결과와 Finding이 perspective를 가진다. 이후 Query 양이나 Drift lifecycle 요구가 생기면
DynamoDB 모델은 versioned Contract 변경으로 전용 entity를 추가할 수 있다.
