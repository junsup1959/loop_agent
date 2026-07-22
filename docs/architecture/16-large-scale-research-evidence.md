---
runtime_injection: false
source_of_truth: [../../skills/research-loop/SKILL.md, ../../agents/context-profiles.toml, ../../scripts/agent_team_state.py]
---

# 대규모 조사 증거

기존 research loop는 brief → source ledger → shard summary → claim/conflict → conclusion의 구조를 유지한다. 긴 원문이나 summary를 합쳐 agent packet에 넣지 않고 artifact로 보존하며 SQLite에는 bounded reference와 digest를 기록한다.

탄력 슬롯은 한 Goal/run에서 하나의 bounded `worker` 또는 `advisory` activation만 허용한다. 결과는 untrusted evidence이며 PM/TA/PL의 gate authority를 대체하지 않는다. nested spawn과 standing approval도 없다.

출처, 대상 OID, 선택 범위, producer, timestamp, digest가 없는 주장은 재사용하지 않는다. 코드 조사에는 Serena semantic evidence를 사용할 수 있지만 required MCP policy와 receipt가 적용된다. 최종 합성은 충돌과 불확실성을 보존한다.

실행 순서는 [대규모 조사 루프](../workflow/16-large-scale-research-loop.md)를 참조한다.
