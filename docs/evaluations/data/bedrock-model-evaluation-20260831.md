# Bedrock 모델 실측 평가

외부 벤치마크·단가가 아닌 이 실행의 유효성, 유효 출력 내 최소 Case 결정 일치율, Assessment score 편차, 지연시간, 토큰 사용량만 사용했습니다. 결정 일치율은 유효 출력만 분모로 계산하며, invalid 실행은 유효율에 별도로 반영했습니다.

## ASSESSMENT

| 후보 모델 | 품질 Gate | 유효 실행 | 유효 출력 내 최소 Case 결정 일치율 | Score 범위 | 중앙 지연 | 중앙 토큰 | 오류 |
|---|---:|---:|---:|---:|---:|---:|---|
| Amazon Nova Micro (`amazon.nova-micro-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 881 ms | 450 | - |
| Qwen Qwen3-Coder-30B-A3B-Instruct (`qwen.qwen3-coder-30b-a3b-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 921 ms | 449 | - |
| Google Gemma 3 4B IT (`google.gemma-3-4b-it`) | PASS | 5/5 (100%) | 100% | 0.0 | 1107 ms | 500 | - |
| Amazon Nova Lite (`amazon.nova-lite-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 1137 ms | 479 | - |
| Amazon Nova Pro (`amazon.nova-pro-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 1160 ms | 449 | - |
| Qwen Qwen3 32B (dense) (`qwen.qwen3-32b-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 1299 ms | 444 | - |
| Z.AI GLM 4.7 Flash (`zai.glm-4.7-flash`) | PASS | 5/5 (100%) | 100% | 0.0 | 1405 ms | 432 | - |
| NVIDIA NVIDIA Nemotron 3 Super 120B A12B (`nvidia.nemotron-super-3-120b`) | PASS | 5/5 (100%) | 100% | 0.0 | 1764 ms | 464 | - |
| OpenAI GPT OSS Safeguard 20B (`openai.gpt-oss-safeguard-20b`) | PASS | 5/5 (100%) | 100% | 0.0 | 1767 ms | 662 | - |
| Meta Llama 3 8B Instruct (`meta.llama3-8b-instruct-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 1996 ms | 405 | - |
| OpenAI gpt-oss-120b (`openai.gpt-oss-120b-1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 2254 ms | 675 | - |
| DeepSeek DeepSeek V3.2 (`deepseek.v3.2`) | PASS | 5/5 (100%) | 100% | 0.0 | 2413 ms | 442 | - |
| Mistral AI Mistral Small (24.02) (`mistral.mistral-small-2402-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 3115 ms | 490 | - |
| Qwen Qwen3 Next 80B A3B (`qwen.qwen3-next-80b-a3b`) | PASS | 5/5 (100%) | 100% | 0.0 | 3281 ms | 442 | - |
| Meta Llama 3 70B Instruct (`meta.llama3-70b-instruct-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 3362 ms | 364 | - |
| Mistral AI Voxtral Small 24B 2507 (`mistral.voxtral-small-24b-2507`) | PASS | 5/5 (100%) | 100% | 0.0 | 3580 ms | 442 | - |
| Google Gemma 3 12B IT (`google.gemma-3-12b-it`) | PASS | 5/5 (100%) | 100% | 0.0 | 3595 ms | 510 | - |
| Moonshot AI Kimi K2.5 (`moonshotai.kimi-k2.5`) | PASS | 5/5 (100%) | 100% | 0.0 | 3669 ms | 431 | - |
| Qwen Qwen3 VL 235B A22B (`qwen.qwen3-vl-235b-a22b`) | PASS | 5/5 (100%) | 100% | 0.0 | 3687 ms | 435 | - |
| Google Gemma 3 27B PT (`google.gemma-3-27b-it`) | PASS | 5/5 (100%) | 100% | 0.0 | 3690 ms | 489 | - |
| Mistral AI Magistral Small 2509 (`mistral.magistral-small-2509`) | PASS | 5/5 (100%) | 100% | 0.0 | 3825 ms | 456 | - |
| Z.AI GLM 4.7 (`zai.glm-4.7`) | PASS | 5/5 (100%) | 100% | 0.0 | 3920 ms | 421 | - |
| Mistral AI Mistral Large (24.02) (`mistral.mistral-large-2402-v1:0`) | PASS | 5/5 (100%) | 100% | 0.0 | 4770 ms | 524 | - |
| Z.AI GLM 5 (`zai.glm-5`) | PASS | 5/5 (100%) | 100% | 0.0 | 10540 ms | 426 | - |
| NVIDIA NVIDIA Nemotron Nano 12B v2 VL BF16 (`nvidia.nemotron-nano-12b-v2`) | FAIL | 3/5 (60%) | 100% | 0.0 | 1459 ms | 460 | JSONDecodeError |
| Mistral AI Devstral 2 123B (`mistral.devstral-2-123b`) | FAIL | 3/5 (60%) | 100% | 0.0 | 3035 ms | 428 | - |
| MiniMax MiniMax M2.5 (`minimax.minimax-m2.5`) | FAIL | 3/5 (60%) | 100% | 0.0 | 10050 ms | 595 | JSONDecodeError |
| Mistral AI Ministral 3B (`mistral.ministral-3-3b-instruct`) | FAIL | 0/5 (0%) | 0% | inf | 1356 ms | 612 | JSONDecodeError |
| Mistral AI Mistral Large 3 (`mistral.mistral-large-3-675b-instruct`) | FAIL | 0/5 (0%) | 0% | inf | 1431 ms | 528 | - |
| NVIDIA Nemotron Nano 3 30B (`nvidia.nemotron-nano-3-30b`) | FAIL | 0/5 (0%) | 0% | inf | 1582 ms | 460 | JSONDecodeError |
| Qwen Qwen3 Coder Next (`qwen.qwen3-coder-next`) | FAIL | 0/5 (0%) | 0% | inf | 5845 ms | 417 | - |
| Cohere Rerank 3.5 (`cohere.rerank-v3-5:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| MiniMax MiniMax M2 (`minimax.minimax-m2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| MiniMax MiniMax M2.1 (`minimax.minimax-m2.1`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Ministral 14B 3.0 (`mistral.ministral-3-14b-instruct`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Ministral 3 8B (`mistral.ministral-3-8b-instruct`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Mistral 7B Instruct (`mistral.mistral-7b-instruct-v0:2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Mistral AI Mixtral 8x7B Instruct (`mistral.mixtral-8x7b-instruct-v0:1`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Mistral AI Voxtral Mini 3B 2507 (`mistral.voxtral-mini-3b-2507`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Moonshot AI Kimi K2 Thinking (`moonshot.kimi-k2-thinking`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| NVIDIA NVIDIA Nemotron Nano 9B v2 (`nvidia.nemotron-nano-9b-v2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI GPT OSS Safeguard 120B (`openai.gpt-oss-safeguard-120b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| TwelveLabs Pegasus v1.2 (`twelvelabs.pegasus-1-2-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Writer Writer Palmyra Vision 7B (`writer.palmyra-vision-7b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ServiceUnavailableException:ServiceUnavailableException, ValidationException:ValidationException |

**선정:** Amazon Nova Micro (`amazon.nova-micro-v1:0`)

선정 이유: 품질 Gate 통과 후보를 유효율, 유효 출력 내 최소 Case 결정 일치율, score 편차, 중앙 지연, 중앙 토큰 순으로 정렬했습니다.

## Parent

| 후보 모델 | 품질 Gate | 유효 실행 | 유효 출력 내 최소 Case 결정 일치율 | Score 범위 | 중앙 지연 | 중앙 토큰 | 오류 |
|---|---:|---:|---:|---:|---:|---:|---|
| Google Gemma 3 4B IT (`google.gemma-3-4b-it`) | PASS | 15/15 (100%) | 100% | - | 659 ms | 164 | - |
| Z.AI GLM 4.7 Flash (`zai.glm-4.7-flash`) | PASS | 15/15 (100%) | 100% | - | 725 ms | 155 | - |
| Mistral AI Mistral Large 3 (`mistral.mistral-large-3-675b-instruct`) | PASS | 15/15 (100%) | 100% | - | 755 ms | 148 | - |
| Qwen Qwen3 32B (dense) (`qwen.qwen3-32b-v1:0`) | PASS | 15/15 (100%) | 100% | - | 771 ms | 167 | - |
| Qwen Qwen3-Coder-30B-A3B-Instruct (`qwen.qwen3-coder-30b-a3b-v1:0`) | PASS | 15/15 (100%) | 100% | - | 804 ms | 152 | - |
| Mistral AI Ministral 14B 3.0 (`mistral.ministral-3-14b-instruct`) | PASS | 15/15 (100%) | 100% | - | 844 ms | 175 | - |
| Qwen Qwen3 Coder Next (`qwen.qwen3-coder-next`) | PASS | 15/15 (100%) | 100% | - | 927 ms | 183 | - |
| NVIDIA NVIDIA Nemotron 3 Super 120B A12B (`nvidia.nemotron-super-3-120b`) | PASS | 15/15 (100%) | 100% | - | 945 ms | 180 | - |
| Mistral AI Voxtral Small 24B 2507 (`mistral.voxtral-small-24b-2507`) | PASS | 15/15 (100%) | 100% | - | 1073 ms | 134 | - |
| Mistral AI Mistral Small (24.02) (`mistral.mistral-small-2402-v1:0`) | PASS | 15/15 (100%) | 100% | - | 1159 ms | 167 | - |
| Mistral AI Magistral Small 2509 (`mistral.magistral-small-2509`) | PASS | 15/15 (100%) | 100% | - | 1166 ms | 139 | - |
| Mistral AI Ministral 3 8B (`mistral.ministral-3-8b-instruct`) | PASS | 15/15 (100%) | 100% | - | 1198 ms | 163 | - |
| OpenAI gpt-oss-120b (`openai.gpt-oss-120b-1:0`) | PASS | 15/15 (100%) | 100% | - | 1219 ms | 298 | - |
| DeepSeek DeepSeek V3.2 (`deepseek.v3.2`) | PASS | 15/15 (100%) | 100% | - | 1233 ms | 161 | - |
| Google Gemma 3 27B PT (`google.gemma-3-27b-it`) | PASS | 15/15 (100%) | 100% | - | 1331 ms | 159 | - |
| Mistral AI Devstral 2 123B (`mistral.devstral-2-123b`) | PASS | 15/15 (100%) | 100% | - | 1339 ms | 157 | - |
| Qwen Qwen3 Next 80B A3B (`qwen.qwen3-next-80b-a3b`) | PASS | 15/15 (100%) | 100% | - | 1412 ms | 170 | - |
| Moonshot AI Kimi K2.5 (`moonshotai.kimi-k2.5`) | PASS | 15/15 (100%) | 100% | - | 1485 ms | 196 | - |
| Mistral AI Mistral Large (24.02) (`mistral.mistral-large-2402-v1:0`) | PASS | 15/15 (100%) | 100% | - | 1537 ms | 169 | - |
| Z.AI GLM 4.7 (`zai.glm-4.7`) | PASS | 15/15 (100%) | 100% | - | 1626 ms | 156 | - |
| Qwen Qwen3 VL 235B A22B (`qwen.qwen3-vl-235b-a22b`) | PASS | 15/15 (100%) | 100% | - | 1646 ms | 163 | - |
| Z.AI GLM 5 (`zai.glm-5`) | PASS | 15/15 (100%) | 100% | - | 2045 ms | 168 | - |
| Mistral AI Ministral 3B (`mistral.ministral-3-3b-instruct`) | PASS | 14/15 (93%) | 100% | - | 554 ms | 171 | JSONDecodeError |
| NVIDIA Nemotron Nano 3 30B (`nvidia.nemotron-nano-3-30b`) | PASS | 14/15 (93%) | 100% | - | 693 ms | 156 | JSONDecodeError |
| Amazon Nova Lite (`amazon.nova-lite-v1:0`) | PASS | 14/15 (93%) | 100% | - | 2093 ms | 155 | - |
| OpenAI gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | FAIL | 11/15 (73%) | 100% | - | 812 ms | 256 | JSONDecodeError |
| Mistral AI Voxtral Mini 3B 2507 (`mistral.voxtral-mini-3b-2507`) | FAIL | 10/15 (67%) | 0% | - | 540 ms | 135 | JSONDecodeError |
| Amazon Nova Micro (`amazon.nova-micro-v1:0`) | FAIL | 10/15 (67%) | 0% | - | 627 ms | 161 | - |
| NVIDIA NVIDIA Nemotron Nano 12B v2 VL BF16 (`nvidia.nemotron-nano-12b-v2`) | FAIL | 10/15 (67%) | 0% | - | 745 ms | 157 | - |
| OpenAI GPT OSS Safeguard 20B (`openai.gpt-oss-safeguard-20b`) | FAIL | 10/15 (67%) | 0% | - | 814 ms | 288 | JSONDecodeError |
| Amazon Nova Pro (`amazon.nova-pro-v1:0`) | FAIL | 10/15 (67%) | 0% | - | 876 ms | 163 | - |
| Google Gemma 3 12B IT (`google.gemma-3-12b-it`) | FAIL | 10/15 (67%) | 0% | - | 1181 ms | 151 | - |
| Meta Llama 3 70B Instruct (`meta.llama3-70b-instruct-v1:0`) | FAIL | 10/15 (67%) | 0% | - | 1335 ms | 145 | - |
| MiniMax MiniMax M2.5 (`minimax.minimax-m2.5`) | FAIL | 4/15 (27%) | 0% | - | 8446 ms | 242 | JSONDecodeError |
| MiniMax MiniMax M2.1 (`minimax.minimax-m2.1`) | FAIL | 2/15 (13%) | 0% | - | 2743 ms | 254 | JSONDecodeError |
| Cohere Rerank 3.5 (`cohere.rerank-v3-5:0`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Meta Llama 3 8B Instruct (`meta.llama3-8b-instruct-v1:0`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | JSONDecodeError |
| MiniMax MiniMax M2 (`minimax.minimax-m2`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Mistral 7B Instruct (`mistral.mistral-7b-instruct-v0:2`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Mistral AI Mixtral 8x7B Instruct (`mistral.mixtral-8x7b-instruct-v0:1`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Moonshot AI Kimi K2 Thinking (`moonshot.kimi-k2-thinking`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | JSONDecodeError |
| NVIDIA NVIDIA Nemotron Nano 9B v2 (`nvidia.nemotron-nano-9b-v2`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI GPT OSS Safeguard 120B (`openai.gpt-oss-safeguard-120b`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | JSONDecodeError |
| TwelveLabs Pegasus v1.2 (`twelvelabs.pegasus-1-2-v1:0`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Writer Writer Palmyra Vision 7B (`writer.palmyra-vision-7b`) | FAIL | 0/15 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |

**선정:** Google Gemma 3 4B IT (`google.gemma-3-4b-it`)

선정 이유: 품질 Gate 통과 후보를 유효율, 유효 출력 내 최소 Case 결정 일치율, 중앙 지연, 중앙 토큰 순으로 정렬했습니다.

## POLICY_QA

| 후보 모델 | 품질 Gate | 유효 실행 | 유효 출력 내 최소 Case 결정 일치율 | Score 범위 | 중앙 지연 | 중앙 토큰 | 오류 |
|---|---:|---:|---:|---:|---:|---:|---|
| Mistral AI Voxtral Mini 3B 2507 (`mistral.voxtral-mini-3b-2507`) | PASS | 5/5 (100%) | 100% | - | 610 ms | 210 | - |
| Qwen Qwen3-Coder-30B-A3B-Instruct (`qwen.qwen3-coder-30b-a3b-v1:0`) | PASS | 5/5 (100%) | 100% | - | 674 ms | 213 | - |
| Z.AI GLM 4.7 Flash (`zai.glm-4.7-flash`) | PASS | 5/5 (100%) | 100% | - | 748 ms | 201 | - |
| Qwen Qwen3 32B (dense) (`qwen.qwen3-32b-v1:0`) | PASS | 5/5 (100%) | 100% | - | 756 ms | 221 | - |
| Mistral AI Ministral 14B 3.0 (`mistral.ministral-3-14b-instruct`) | PASS | 5/5 (100%) | 100% | - | 766 ms | 214 | - |
| Amazon Nova Pro (`amazon.nova-pro-v1:0`) | PASS | 5/5 (100%) | 100% | - | 815 ms | 234 | - |
| NVIDIA NVIDIA Nemotron 3 Super 120B A12B (`nvidia.nemotron-super-3-120b`) | PASS | 5/5 (100%) | 100% | - | 869 ms | 217 | - |
| Google Gemma 3 4B IT (`google.gemma-3-4b-it`) | PASS | 5/5 (100%) | 100% | - | 996 ms | 230 | - |
| Qwen Qwen3 Next 80B A3B (`qwen.qwen3-next-80b-a3b`) | PASS | 5/5 (100%) | 100% | - | 1018 ms | 213 | - |
| DeepSeek DeepSeek V3.2 (`deepseek.v3.2`) | PASS | 5/5 (100%) | 100% | - | 1065 ms | 192 | - |
| Qwen Qwen3 Coder Next (`qwen.qwen3-coder-next`) | PASS | 5/5 (100%) | 100% | - | 1171 ms | 213 | - |
| OpenAI gpt-oss-120b (`openai.gpt-oss-120b-1:0`) | PASS | 5/5 (100%) | 100% | - | 1467 ms | 405 | - |
| Google Gemma 3 12B IT (`google.gemma-3-12b-it`) | PASS | 5/5 (100%) | 100% | - | 1597 ms | 231 | - |
| Qwen Qwen3 VL 235B A22B (`qwen.qwen3-vl-235b-a22b`) | PASS | 5/5 (100%) | 100% | - | 1619 ms | 213 | - |
| Google Gemma 3 27B PT (`google.gemma-3-27b-it`) | PASS | 5/5 (100%) | 100% | - | 1638 ms | 231 | - |
| Z.AI GLM 4.7 (`zai.glm-4.7`) | PASS | 5/5 (100%) | 100% | - | 2081 ms | 201 | - |
| MiniMax MiniMax M2.5 (`minimax.minimax-m2.5`) | PASS | 5/5 (100%) | 100% | - | 5373 ms | 349 | - |
| NVIDIA NVIDIA Nemotron Nano 12B v2 VL BF16 (`nvidia.nemotron-nano-12b-v2`) | FAIL | 4/5 (80%) | 100% | - | 800 ms | 224 | JSONDecodeError |
| Mistral AI Devstral 2 123B (`mistral.devstral-2-123b`) | FAIL | 4/5 (80%) | 100% | - | 1440 ms | 214 | TypeError |
| Moonshot AI Kimi K2.5 (`moonshotai.kimi-k2.5`) | FAIL | 4/5 (80%) | 100% | - | 1483 ms | 205 | - |
| Mistral AI Ministral 3 8B (`mistral.ministral-3-8b-instruct`) | FAIL | 1/5 (20%) | 100% | - | 562 ms | 210 | TypeError |
| Amazon Nova Lite (`amazon.nova-lite-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 623 ms | 234 | - |
| Amazon Nova Micro (`amazon.nova-micro-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 649 ms | 230 | - |
| NVIDIA Nemotron Nano 3 30B (`nvidia.nemotron-nano-3-30b`) | FAIL | 0/5 (0%) | 0% | - | 1137 ms | 252 | - |
| Mistral AI Voxtral Small 24B 2507 (`mistral.voxtral-small-24b-2507`) | FAIL | 0/5 (0%) | 0% | - | 1455 ms | 206 | - |
| Mistral AI Magistral Small 2509 (`mistral.magistral-small-2509`) | FAIL | 0/5 (0%) | 0% | - | 1508 ms | 210 | - |
| Meta Llama 3 70B Instruct (`meta.llama3-70b-instruct-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 1535 ms | 192 | - |
| Mistral AI Mistral Large (24.02) (`mistral.mistral-large-2402-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 2220 ms | 262 | - |
| Cohere Rerank 3.5 (`cohere.rerank-v3-5:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Meta Llama 3 8B Instruct (`meta.llama3-8b-instruct-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| MiniMax MiniMax M2 (`minimax.minimax-m2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| MiniMax MiniMax M2.1 (`minimax.minimax-m2.1`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Ministral 3B (`mistral.ministral-3-3b-instruct`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | TypeError |
| Mistral AI Mistral 7B Instruct (`mistral.mistral-7b-instruct-v0:2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Mistral AI Mistral Large 3 (`mistral.mistral-large-3-675b-instruct`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | TypeError |
| Mistral AI Mistral Small (24.02) (`mistral.mistral-small-2402-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Mixtral 8x7B Instruct (`mistral.mixtral-8x7b-instruct-v0:1`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Moonshot AI Kimi K2 Thinking (`moonshot.kimi-k2-thinking`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| NVIDIA NVIDIA Nemotron Nano 9B v2 (`nvidia.nemotron-nano-9b-v2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI GPT OSS Safeguard 120B (`openai.gpt-oss-safeguard-120b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI GPT OSS Safeguard 20B (`openai.gpt-oss-safeguard-20b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| TwelveLabs Pegasus v1.2 (`twelvelabs.pegasus-1-2-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Writer Writer Palmyra Vision 7B (`writer.palmyra-vision-7b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Z.AI GLM 5 (`zai.glm-5`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | TypeError |

**선정:** Mistral AI Voxtral Mini 3B 2507 (`mistral.voxtral-mini-3b-2507`)

선정 이유: 품질 Gate 통과 후보를 유효율, 유효 출력 내 최소 Case 결정 일치율, 중앙 지연, 중앙 토큰 순으로 정렬했습니다.

## REMEDIATION_DEPLOYMENT

| 후보 모델 | 품질 Gate | 유효 실행 | 유효 출력 내 최소 Case 결정 일치율 | Score 범위 | 중앙 지연 | 중앙 토큰 | 오류 |
|---|---:|---:|---:|---:|---:|---:|---|
| Mistral AI Devstral 2 123B (`mistral.devstral-2-123b`) | PASS | 5/5 (100%) | 100% | - | 5563 ms | 664 | - |
| Z.AI GLM 5 (`zai.glm-5`) | FAIL | 4/5 (80%) | 100% | - | 6903 ms | 589 | - |
| Qwen Qwen3-Coder-30B-A3B-Instruct (`qwen.qwen3-coder-30b-a3b-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 1002 ms | 628 | - |
| Mistral AI Ministral 3B (`mistral.ministral-3-3b-instruct`) | FAIL | 0/5 (0%) | 0% | - | 1433 ms | 666 | JSONDecodeError |
| Mistral AI Mistral Large 3 (`mistral.mistral-large-3-675b-instruct`) | FAIL | 0/5 (0%) | 0% | - | 1534 ms | 658 | - |
| Mistral AI Voxtral Mini 3B 2507 (`mistral.voxtral-mini-3b-2507`) | FAIL | 0/5 (0%) | 0% | - | 1734 ms | 697 | - |
| Amazon Nova Micro (`amazon.nova-micro-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 1738 ms | 777 | - |
| Amazon Nova Lite (`amazon.nova-lite-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 1749 ms | 726 | - |
| Mistral AI Ministral 3 8B (`mistral.ministral-3-8b-instruct`) | FAIL | 0/5 (0%) | 0% | - | 1754 ms | 662 | JSONDecodeError |
| Z.AI GLM 4.7 Flash (`zai.glm-4.7-flash`) | FAIL | 0/5 (0%) | 0% | - | 1793 ms | 620 | - |
| Qwen Qwen3 Coder Next (`qwen.qwen3-coder-next`) | FAIL | 0/5 (0%) | 0% | - | 1941 ms | 624 | - |
| Google Gemma 3 4B IT (`google.gemma-3-4b-it`) | FAIL | 0/5 (0%) | 0% | - | 1941 ms | 698 | - |
| Qwen Qwen3 32B (dense) (`qwen.qwen3-32b-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 1956 ms | 664 | - |
| NVIDIA Nemotron Nano 3 30B (`nvidia.nemotron-nano-3-30b`) | FAIL | 0/5 (0%) | 0% | - | 2060 ms | 662 | JSONDecodeError |
| Amazon Nova Pro (`amazon.nova-pro-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 2138 ms | 780 | - |
| NVIDIA NVIDIA Nemotron Nano 12B v2 VL BF16 (`nvidia.nemotron-nano-12b-v2`) | FAIL | 0/5 (0%) | 0% | - | 2242 ms | 666 | - |
| OpenAI gpt-oss-120b (`openai.gpt-oss-120b-1:0`) | FAIL | 0/5 (0%) | 0% | - | 2614 ms | 975 | JSONDecodeError |
| NVIDIA NVIDIA Nemotron 3 Super 120B A12B (`nvidia.nemotron-super-3-120b`) | FAIL | 0/5 (0%) | 0% | - | 3007 ms | 715 | - |
| OpenAI gpt-oss-20b (`openai.gpt-oss-20b-1:0`) | FAIL | 0/5 (0%) | 0% | - | 3043 ms | 1024 | JSONDecodeError |
| DeepSeek DeepSeek V3.2 (`deepseek.v3.2`) | FAIL | 0/5 (0%) | 0% | - | 3705 ms | 650 | - |
| OpenAI GPT OSS Safeguard 20B (`openai.gpt-oss-safeguard-20b`) | FAIL | 0/5 (0%) | 0% | - | 3892 ms | 1000 | JSONDecodeError |
| Moonshot AI Kimi K2.5 (`moonshotai.kimi-k2.5`) | FAIL | 0/5 (0%) | 0% | - | 4890 ms | 581 | - |
| MiniMax MiniMax M2.1 (`minimax.minimax-m2.1`) | FAIL | 0/5 (0%) | 0% | - | 5186 ms | 898 | - |
| Qwen Qwen3 Next 80B A3B (`qwen.qwen3-next-80b-a3b`) | FAIL | 0/5 (0%) | 0% | - | 5242 ms | 616 | - |
| Qwen Qwen3 VL 235B A22B (`qwen.qwen3-vl-235b-a22b`) | FAIL | 0/5 (0%) | 0% | - | 5722 ms | 637 | - |
| Mistral AI Mistral Small (24.02) (`mistral.mistral-small-2402-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 5998 ms | 763 | - |
| Google Gemma 3 12B IT (`google.gemma-3-12b-it`) | FAIL | 0/5 (0%) | 0% | - | 6001 ms | 749 | - |
| Mistral AI Voxtral Small 24B 2507 (`mistral.voxtral-small-24b-2507`) | FAIL | 0/5 (0%) | 0% | - | 6131 ms | 663 | - |
| Mistral AI Magistral Small 2509 (`mistral.magistral-small-2509`) | FAIL | 0/5 (0%) | 0% | - | 6205 ms | 659 | - |
| Google Gemma 3 27B PT (`google.gemma-3-27b-it`) | FAIL | 0/5 (0%) | 0% | - | 6659 ms | 751 | - |
| Mistral AI Mistral Large (24.02) (`mistral.mistral-large-2402-v1:0`) | FAIL | 0/5 (0%) | 0% | - | 7839 ms | 745 | - |
| Z.AI GLM 4.7 (`zai.glm-4.7`) | FAIL | 0/5 (0%) | 0% | - | 8480 ms | 587 | - |
| MiniMax MiniMax M2.5 (`minimax.minimax-m2.5`) | FAIL | 0/5 (0%) | 0% | - | 17922 ms | 928 | JSONDecodeError |
| Cohere Rerank 3.5 (`cohere.rerank-v3-5:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Meta Llama 3 70B Instruct (`meta.llama3-70b-instruct-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Meta Llama 3 8B Instruct (`meta.llama3-8b-instruct-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| MiniMax MiniMax M2 (`minimax.minimax-m2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Ministral 14B 3.0 (`mistral.ministral-3-14b-instruct`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| Mistral AI Mistral 7B Instruct (`mistral.mistral-7b-instruct-v0:2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Mistral AI Mixtral 8x7B Instruct (`mistral.mixtral-8x7b-instruct-v0:1`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Moonshot AI Kimi K2 Thinking (`moonshot.kimi-k2-thinking`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| NVIDIA NVIDIA Nemotron Nano 9B v2 (`nvidia.nemotron-nano-9b-v2`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| OpenAI GPT OSS Safeguard 120B (`openai.gpt-oss-safeguard-120b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | JSONDecodeError |
| TwelveLabs Pegasus v1.2 (`twelvelabs.pegasus-1-2-v1:0`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ValidationException:ValidationException |
| Writer Writer Palmyra Vision 7B (`writer.palmyra-vision-7b`) | FAIL | 0/5 (0%) | 0% | - | - ms | - | ServiceUnavailableException:ServiceUnavailableException, ValidationException:ValidationException |

**선정:** Mistral AI Devstral 2 123B (`mistral.devstral-2-123b`)

선정 이유: 품질 Gate 통과 후보를 유효율, 유효 출력 내 최소 Case 결정 일치율, 중앙 지연, 중앙 토큰 순으로 정렬했습니다.
