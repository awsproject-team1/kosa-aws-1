# M4 Golden Dataset customer-sandbox release gate

이 runbook은 ADR-0021/0022에 따라 M4 C 품질 Gate를 재현한다. 로컬 fixture gate와 실제 customer sandbox evidence를 구분한다.

## 사전 조건

- `fixtures/m4/demo_policy_coverage.json` 검증 통과
- protected customer sandbox에 현재 platform revision 배포
- `assessment-nova-lite-m1-v3` 전체 Model Profile과 `m1-three-perspective-v1` rubric 사용
- D demo apply 완료 및 Post-Deploy artifact set 고정
- private evidence 보관 위치와 release reviewer 지정

사전 점검(dry-run, 외부 호출 없음):

```bash
python3 scripts/evaluate_m4_golden_release_gate.py
```

정상 dry-run은 `EXTERNAL_EVIDENCE_REQUIRED`, Case 18, 반복 5, Bedrock call 60, Code-derived DRIFT 30을 출력한다. 이 결과는 release PASS가 아니다.

## A/D → C observation handoff

A의 customer runtime exporter는 다음 top-level JSON을 private 파일로 만든다. 값은 예시 placeholder이며 실제 identifier/credential을 문서나 Git에 넣지 않는다.

```json
{
  "schema_version": "m4-golden-observations-v1",
  "execution_id": "opaque-release-execution-id",
  "generated_at": "2026-09-03T00:00:00+00:00",
  "scenario_id": "wordpress-lamp-s3-governance-v1",
  "runtime_mode": "CUSTOMER_SANDBOX",
  "platform_commit_sha": "<40 lowercase hex>",
  "repository_commit_sha256": "<64 lowercase hex>",
  "deployment_id_sha256": "<64 lowercase hex>",
  "artifact_set_sha256": "<64 lowercase hex>",
  "model_profile": {
    "model_profile_id": "assessment-nova-lite-m1-v3",
    "role": "ASSESSMENT",
    "region": "us-east-1",
    "model_id": "amazon.nova-lite-v1:0",
    "prompt_version": "assessment-three-perspective-rubric-v3",
    "rubric_version": "m1-three-perspective-v1",
    "golden_dataset_version": "m3-s3-initial-post-deploy-six-rule-three-perspective-v1"
  },
  "observations": []
}
```

각 observation은 exact case/run/rule version/phase/perspective, validated status/score/evidence, Model Profile/rubric/scoring mode, resource/artifact/output hash, execution kind, 사용량과 안정된 오류 코드만 가진다. 실행 정본과 exact field allow-list는 `apps/backend/assessment/release_quality.py`다.

- IAC/AWS_ACTUAL 성공 observation: `execution_kind=BEDROCK`, latency/input/output token과 output hash 필수
- DRIFT observation: `execution_kind=CODE_DERIVED`, latency/token은 `null`, 같은 Rule/run의 IAC/Actual과 결정적으로 일치
- provider 실패: raw error message 없이 stable `error_code`; Gate는 실패
- 모든 Bedrock 호출이 실패해 성공 latency가 없으면 입력은 완전한 품질 미달로 처리하고,
  공개 report의 `bedrock_p95_latency_ms`는 `null`이며 CLI는 exit 1을 반환한다.
- 금지: raw Prompt/response/rationale, 실제 resource ID, account/Role/credential, repository URL, policy/IaC body

Private input은 public repository에 커밋하지 않는다. 보호 저장소의 object version/digest와 run URL을 release packet에 기록한다.

## Gate 실행

```bash
python3 scripts/evaluate_m4_golden_release_gate.py \
  --observations /private/path/m4-golden-observations.json \
  --output-dir build/m4-release
```

Exit code:

- `0`: PASS 또는 dry-run 계획 출력(dry-run은 상태가 `EXTERNAL_EVIDENCE_REQUIRED`)
- `1`: 완전한 입력이지만 품질 목표 미달
- `2`: schema/profile/case/run/DRIFT binding이 잘못되거나 파일을 읽을 수 없음

생성되는 `m4-golden-release-report.json`과 `.md`만 공개 release 첨부 후보이며, 첨부 전 reviewer가 private bundle digest와 protected run을 대조한다.

## 차단 기준

각 Case, 각 perspective, 전체에 모두 적용한다.

- status 정확도 ≥ 90%
- score 기대 범위 정확도 ≥ 90%
- Evidence 정확도 ≥ 90%
- 동일 Case 판정 일치율 ≥ 90%
- score 최대 편차 ≤ 10
- `EXECUTION_ERROR`/provider error = 0
- 누락/추가/중복 observation = 0

5회 반복에서는 한 번의 실패도 80%이므로 해당 Case가 실패한다. 목표를 낮추지 않는다. 미달 시 prompt/rubric/Golden Case를 version-up해 재승인하거나, score 편차가 지속될 때만 ADR-0003의 Anchor 절차를 따른다.

## A 관측·비용 및 D 실행 증적 결합

C report의 `execution_id`, `platform_commit_sha`, repository/deployment/artifact hash를 키로 사용한다.

- A 관측 자료: 같은 execution의 Bedrock 호출 60, token/p95, Queue/DLQ/checkpoint, Lambda/storage 비용
- D 실행 자료: 같은 demo commit/deployment의 plan/apply/approval과 Post-Deploy artifact set
- C 품질 자료: observation digest와 18 Case 품질 결과

세 자료가 같은 실행에 결합되지 않거나 protected run을 reviewer가 확인하지 못하면 M4 release gate는 미충족이다.
