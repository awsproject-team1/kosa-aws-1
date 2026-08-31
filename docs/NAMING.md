# Naming

- Branch: `type/kebab-case` (`feature/ai-evaluator-anchor-scoring`)
- Commit: Conventional Commit (`feat:`, `fix:`, `docs:` 등)
- Python: module/function `snake_case`, class `PascalCase`
- TypeScript: value/function `camelCase`, React component `PascalCase`
- Environment variable: `UPPER_SNAKE_CASE`
- AWS resource: `<project>-<env>-<component>`
- IDs: API와 저장소 모두 소문자 snake_case field 이름을 기본으로 한다.

## M0 CloudFormation parameters

- `ProjectName`: 3-40 lowercase letters, numbers, or hyphens; starts and ends with a letter or number.
- `ProjectName` must not start with the S3-reserved prefixes `xn--`, `sthree-`, or `amzn-s3-demo-`.
- `Environment`: 2-12 lowercase letters, numbers, or hyphens; starts and ends with a letter or number.
- `ProjectName` must qualify the deployment sufficiently to keep the derived artifact bucket globally unique.
