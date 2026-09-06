# GovLens Demo Video VOD

이 구성은 제품 SPA와 분리된 수업 시연용 VOD 경로다. 원본 영상은 Git에 커밋하지 않는다.

```text
source S3 ObjectCreated
  → EventBridge
  → submit Lambda
  → MediaConvert (HLS 1080p / 720p / 480p)
  → private output S3
  → CloudFront OAC
  → browser HLS player
```

MediaConvert의 `COMPLETE`와 `ERROR`는 EventBridge가 SNS topic으로 전달한다. 이메일 parameter를
지정했다면 AWS가 보낸 subscription confirmation을 먼저 승인해야 한다.

## Deployment

정본은 `infrastructure/cloudformation/demo-video-vod.yaml`이며
`.github/workflows/deploy-demo-video.yml`만 실제 스택을 배포한다. Stack과 OIDC Role scope는
`kosa-governance-demo-video`로 고정한다.

기존 bootstrap stack은 먼저 최신 `m1-customer-bootstrap-roles.yaml`로 갱신되어야 한다. 이 변경은
GitHub deployment role에 demo stack 하나의 CloudFormation 접근과 output bucket의 player 게시
권한을 추가하고, CloudFormation execution role에 CloudFront/SNS provisioning을 추가한다.
MediaConvert job 제출 권한은 CloudFormation 역할이 아니라 전용 Lambda runtime 역할에만 있다.

## Source upload

배포가 완료되면 원본을 다음 exact key로 업로드한다.

```bash
aws s3 cp GovLens.mp4 \
  s3://kosa-governance-sandbox-video-source-369676914736/source/GovLens.mp4 \
  --content-type video/mp4 \
  --profile mfa \
  --region us-east-1
```

EventBridge delivery와 MediaConvert job은 비동기다. 완료 뒤 stack의 `PlayerUrl`을 열면
`/videos/GovLens/master.m3u8`을 adaptive HLS로 재생한다.

## Cleanup and cost

MediaConvert는 변환한 output duration에, S3는 저장량·요청에, CloudFront는 전송량·요청에 따라
비용이 발생한다. Stack의 두 bucket은 `Retain`이므로 stack 삭제 후에도 원본과 결과는 남는다.
삭제가 필요하면 사람이 보존 여부를 확인한 뒤 bucket objects와 bucket을 별도로 정리한다.
