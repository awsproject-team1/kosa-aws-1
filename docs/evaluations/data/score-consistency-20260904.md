# Continuous score consistency — live measurement, 2026-09-04

Model Profile `assessment-nova-lite-m1-v3` (`amazon.nova-lite-v1:0`, rubric `m1-three-perspective-v1`),
customer sandbox account 369676914736, `us-east-1`. Producer: `scripts/measure_score_consistency.py`
with the production evaluator (`BedrockStructuredEvaluator`, same prompt and inference config the
Assessment Worker uses). All documents are synthetic; no customer content is stored here.

This file records what was measured, not a pass/fail gate. No tolerance is decided here (ADR-0003).

## Headline

**The continuous 0–100 score carries no gradation on this model.** Across 120 runs of the final
measurement the only two values ever returned were `0` and `100`, including on documents that are
genuinely partly compliant. Repeat stability is therefore perfect and uninformative: status
agreement 1.0 and range 0 on every case.

Three things had to be ruled out before that claim could be made, and each was ruled out by
measurement rather than by reasoning:

| Suspected cause | Test | Result |
| --- | --- | --- |
| The cases are all-or-nothing, so gradation was never asked for | added 4 partially compliant documents (S3 2/4 and 3/4 flags, ALB HTTPS + plaintext HTTP, private RDS with 3306 open to the world) | still only 0 or 100 |
| The prompt tells the model to keep scores at the extremes | A/B against the `dev` prompt (578d20e), which contains no scoring guidance at all | identical 0/100 distribution |
| An explicit instruction would produce gradation | added "reserve 0 and 100 for a wholly violating or wholly satisfying resource, and place a partially satisfied resource between the extremes" | identical 0/100 distribution |

## Runs

| # | Configuration | Outcome |
| --- | --- | --- |
| 1 | 20 cases × 5, prompt as first written on this branch | 19 contract errors; ALB HTTPS Actual judged FAIL 5/5 |
| 2 | diagnosis A/B, 3 runs per arm | ALB misjudgment traced to the document key, not the model |
| 3 | 20 cases × 5, after the evidence and ALB fixes | 0 contract errors, 18/20 expected status, all scores 0 or 100 |
| 4 | 24 cases × 5, after the prompt revert, adds the partial-compliance cases | 2 contract errors, 20/24 expected status, all scores 0 or 100 |

### Run 1 → the two defects it exposed

- **Anchored evidence was rejected.** The model cites `terraform:{path}#{resource address}` — the
  form `fixtures/m1/golden_dataset_cases.json` itself expects — while the allow-list holds the file
  locator. 19 of 25 IAC runs died on `evidence reference is outside approved evidence`. In the live
  Worker that exception is an evaluation failure, an SQS retry, and eventually the DLQ. Fixed by
  accepting an anchor inside an already approved `terraform:`/`aws:`/`s3://` locator; an anchor on
  an unapproved file, and any policy locator that is not an exact match, are still refused.
- **A compliant ALB was failed 5/5.** The Actual document nested the load balancer's attribute map
  under `attributes.attributes`. A/B with only that key renamed: `FAIL` 3/3 before,
  `PASS` 3/3 after. Renamed to `load_balancer_attributes` (Control Catalog path synced).

### Run 4 — final numbers

Every case: 5 runs, status agreement 1.0, finding agreement 1.0, range 0, stdev 0, max pairwise
difference 0. Severe-overestimation candidates (a `FAIL` scored above the Golden violation ceiling
of 30): none. Non-judgment statuses carrying a non-zero score: none.

Cases whose result contradicts the rule text:

| case | rule | model | expected by the rule | reading |
| --- | --- | --- | --- | --- |
| `alb-https-plus-http-actual` | ALB-HTTPS-001 (HTTPS/TLS listeners **only**) | `PASS` 100, 5/5 | `FAIL` | **False negative.** A plaintext HTTP listener is present and the model reports the HTTPS one. |
| `s3-three-of-four-actual` | S3-PUBLIC-001 (apply Block Public Access) | `PASS` 100 ×2, `FAIL` 0 ×3 | `FAIL` | **Unstable false negative.** `RestrictPublicBuckets` is false. In an earlier probe without the `policy.IsPublic` field this case was `PASS` 5/5, so the extra evidence field is what pulls some runs to `FAIL`. |
| `ec2-public-ip-actual` | EC2-PUBLIC-IP-001 (no public IP on a private-tier instance) | `OUT_OF_SCOPE` 5/5 | `FAIL` | Adapter gap, not model evasion: the document has no way to say the subnet is private. See below. |
| `ec2-private-actual` | same | `OUT_OF_SCOPE` 5/5 | `PASS` | same |

Residual contract errors: 2 of 5 runs of `ec2-public-ip-iac` cited `compute.tf#L8` with the
`terraform:` namespace dropped. Rejected on purpose — accepting a bare path would admit locators
outside the approved vocabulary.

## The prompt change that cost accuracy

The first version of this branch also told the model, in prose, that `MANUAL_REVIEW`,
`INSUFFICIENT_EVIDENCE` and `OUT_OF_SCOPE` each carry score 0. The runtime pins those scores in
code (`_normalized_score`), so the sentence bought nothing — and it measurably hurt:

| prompt | RDS-ACCESS-001, private instance with 3306 open to 0.0.0.0/0, n=8 |
| --- | --- |
| `dev` (no enumeration) | `FAIL` 5, `OUT_OF_SCOPE` 3 |
| this branch, first version | `FAIL` 0, `OUT_OF_SCOPE` 8 |
| final (enumeration removed, gradation sentence kept), n=5 | `FAIL` 5, `OUT_OF_SCOPE` 0 |

Naming the evasive statuses alongside their score made the model pick them. The enumeration was
removed; score pinning stays in code where it belongs.

## Known gap this measurement documents

`EC2-PUBLIC-IP-001` governs instances **in a private tier**, but the EC2 Actual adapter projects
only `SubnetId` — never whether that subnet assigns public IPs. With no evidence for the rule's
own precondition the model answers `OUT_OF_SCOPE`, which is a defensible reading of the document it
was given. Closing this needs `ec2:DescribeSubnets` in the customer read role plus a
`MapPublicIpOnLaunch` projection, so it is deliberately left open rather than patched around.

## Reproducing

```bash
AWS_PROFILE=<mfa session> python scripts/measure_score_consistency.py \
  --repetitions 5 --output consistency.json --markdown consistency.md
```

`--dry-run` exercises the plumbing with a deterministic fake model; its numbers are not evidence.
