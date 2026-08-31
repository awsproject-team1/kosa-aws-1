# Naming

- Branch: `type/kebab-case` (`feature/ai-evaluator-anchor-scoring`)
- Commit: Conventional Commit (`feat:`, `fix:`, `docs:` 등)
- Python: module/function `snake_case`, class `PascalCase`
- TypeScript: value/function `camelCase`, React component `PascalCase`
- Environment variable: `UPPER_SNAKE_CASE`
- AWS resource: `<project>-<env>-<component>`
- IDs: API와 저장소 모두 소문자 snake_case field 이름을 기본으로 한다.

## M0 CloudFormation parameters

- `ProjectName`: 2-31 lowercase letters, digits, or hyphens; starts with a letter and ends with a letter or digit.
- `ProjectName` must not start with the S3-reserved prefixes `xn--`, `sthree-`, or `amzn-s3-demo-`.
- `Environment`: 2-8 lowercase letters, digits, or hyphens; starts with a letter and ends with a letter or digit.
- The globally named artifact bucket is the AWS-resource naming exception:
  `<project>-<env>-artifacts-<account-id>`.
- The parameter maxima keep the derived artifact bucket within S3's 63-character limit. The
  account ID suffix reduces collision risk, but the resulting bucket name must still be globally unique.
