# C4 System Context

사용자는 Cloud Governance & Compliance Platform에서 정책 프로필과 고객 Terraform Repository를 선택해 AWS 환경을 평가한다. 플랫폼은 고객 AWS Account에 배포되고, GitHub App을 통해 고객 IaC Repository와 통합하며, GitHub Actions가 승인된 변경만 apply한다. AWS Resource Tool은 고객 워크로드(EC2/RDS/ALB/S3)를 읽기 전용으로 조회한다.
