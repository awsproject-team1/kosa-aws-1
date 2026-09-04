# Continuous score consistency measurement

- model_profile_id: `assessment-nova-lite-m1-v3`
- model_id: `amazon.nova-lite-v1:0` · prompt_version: `assessment-three-perspective-rubric-v3` · rubric_version: `m1-three-perspective-v1`
- repetitions: 3 · dry_run: False

## Self-agreement per case

| case | rule | perspective | expected | runs | scores | mean | min | max | range | stdev | status agreement | expected status accuracy | finding agreement | max pairwise | contract errors | severe overestimation candidates |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rds-public-iac | RDS-PUBLIC-001 | IAC | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| rds-private-iac | RDS-PUBLIC-001 | IAC | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| rds-public-actual | RDS-PUBLIC-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| rds-private-actual | RDS-PUBLIC-001 | AWS_ACTUAL | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| rds-unencrypted-actual | RDS-ENCRYPT-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| rds-encrypted-actual | RDS-ENCRYPT-001 | AWS_ACTUAL | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-public-iac | S3-PUBLIC-001 | IAC | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-blocked-iac | S3-PUBLIC-001 | IAC | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-public-actual | S3-PUBLIC-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-blocked-actual | S3-PUBLIC-001 | AWS_ACTUAL | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| alb-http-iac | ALB-HTTPS-001 | IAC | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| alb-https-iac | ALB-HTTPS-001 | IAC | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| alb-http-actual | ALB-HTTPS-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| alb-https-actual | ALB-HTTPS-001 | AWS_ACTUAL | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| ec2-public-ip-iac | EC2-PUBLIC-IP-001 | IAC | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| ec2-private-iac | EC2-PUBLIC-IP-001 | IAC | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| ec2-public-ip-actual | EC2-PUBLIC-IP-001 | AWS_ACTUAL | FAIL | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0 | - |
| ec2-private-actual | EC2-PUBLIC-IP-001 | AWS_ACTUAL | PASS | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-two-of-four-actual | S3-PUBLIC-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| s3-three-of-four-actual | S3-PUBLIC-001 | AWS_ACTUAL | FAIL | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0 | - |
| alb-https-plus-http-actual | ALB-HTTPS-001 | AWS_ACTUAL | FAIL | 3 | 100, 100, 100 | 100.0 | 100.0 | 100.0 | 0.0 | 0.0 | 1.0 | 0.0 | 1.0 | 0.0 | 0 | - |
| rds-private-open-sg-actual | RDS-ACCESS-001 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| phrasing-a-rds-public-actual | CUST-RDS_NOT_PUBLIC-a1 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |
| phrasing-b-rds-public-actual | CUST-RDS_NOT_PUBLIC-b2 | AWS_ACTUAL | FAIL | 3 | 0, 0, 0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 1.0 | 1.0 | 1.0 | 0.0 | 0 | - |

## Expected transitions (before → after)

| before | after | before mean | after mean | before status | after status | direction ok |
| --- | --- | --- | --- | --- | --- | --- |
| rds-public-iac | rds-private-iac | 0.0 | 100.0 | FAIL | PASS | True |
| rds-public-actual | rds-private-actual | 0.0 | 100.0 | FAIL | PASS | True |
| rds-unencrypted-actual | rds-encrypted-actual | 0.0 | 100.0 | FAIL | PASS | True |
| s3-public-iac | s3-blocked-iac | 0.0 | 100.0 | FAIL | PASS | True |
| s3-public-actual | s3-blocked-actual | 0.0 | 100.0 | FAIL | PASS | True |
| alb-http-iac | alb-https-iac | 0.0 | 100.0 | FAIL | PASS | True |
| alb-http-actual | alb-https-actual | 0.0 | 100.0 | FAIL | PASS | True |
| ec2-public-ip-iac | ec2-private-iac | 0.0 | 100.0 | FAIL | PASS | True |
| ec2-public-ip-actual | ec2-private-actual | 100.0 | 100.0 | PASS | PASS | True |

## Policy phrasing invariance

| case a | case b | mean a | mean b | |Δ| | status a | status b |
| --- | --- | --- | --- | --- | --- | --- |
| phrasing-a-rds-public-actual | phrasing-b-rds-public-actual | 0.0 | 0.0 | 0.0 | FAIL | FAIL |

## Attribute-order invariance (no model call)

- rds-public-iac: prompt bytes identical = True
- rds-public-actual: prompt bytes identical = True
- rds-unencrypted-actual: prompt bytes identical = True
- s3-public-iac: prompt bytes identical = True
- s3-public-actual: prompt bytes identical = True
- alb-http-iac: prompt bytes identical = True
- alb-http-actual: prompt bytes identical = True
- ec2-public-ip-iac: prompt bytes identical = True
- ec2-public-ip-actual: prompt bytes identical = True

## Contract checks

- runs with contract errors: 0
- non-judgment statuses carrying a non-zero score: 0
- severe overestimation candidates (FAIL with score > 30, the Golden violation-case ceiling): 0
