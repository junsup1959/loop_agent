---
runtime_injection: false
source_of_truth: [../../skills/research-loop/SKILL.md, ../../agents/context-profiles.toml, ../../agents/capabilities.toml]
---

# 대규모 조사 루프

1. PM/PL이 질문, 범위, source policy, 종료 조건을 brief로 고정한다.
2. source ledger에 provenance와 대상 OID를 기록한다.
3. bounded shard를 worker activation에 배정한다.
4. summary를 artifact로 저장하고 claim/conflict record로 정규화한다.
5. TA/PL이 상충 근거와 누락을 검증한다.
6. 결론은 source ref, confidence, unresolved conflict를 포함한다.

탄력 슬롯은 Goal/run당 하나의 worker 또는 advisory만 실행하며 nested spawn·approval·merge 권한이 없다. Serena/Sequential Thinking 사용은 MCP policy와 transition binding을 따르고 receipt를 남긴다.

조사 결과가 구현 입력이 되면 그대로 source authority가 되지 않는다. PL이 bounded work item과 activation contract로 다시 발급한다.
