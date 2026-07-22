---
runtime_injection: false
source_of_truth: [../../agents/mcp-policy.toml, ../../agents/serena-knowledge-policy.toml, ../../scripts/agent_team_context.py]
---

# 조사와 source evidence

조사는 registered repository와 exact target OID를 먼저 고정한다. 파일·symbol·reference·impact 탐색은 transition이 허용한 Serena 도구만 사용하며 사용이 필수인 경우 receipt가 없으면 결과를 인정하지 않는다.

Serena와 Sequential Thinking은 optional 권장 도구가 아니다. 둘 다 healthy 상태와 required tool을 preflight하며 fallback이 없다. coding/review 전이의 Serena allowlist에는 `initial_instructions`가 포함된다.

큰 source excerpt와 빠르게 변하는 code summary는 artifact로 저장한다. SQLite에는 repository, OID, 범위, producer, ref, SHA-256을 남긴다. shared Serena memory에는 active task나 current OID를 기록하지 않는다.
