---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_state.py, ../../scripts/agent_team_message_viewer.py, ../../agents/contracts/schemas/activation-result.schema.json]
---

# 관측과 감사 데이터

Goal, run, work item revision, activation, attempt, contract, repository, lease, OID, worker fingerprint, result와 receipt ID가 상관관계 축이다. 감사자는 “누가 어떤 capability로 어느 OID를 어떤 계약과 도구로 처리했는가”를 SQLite와 artifact digest로 재구성할 수 있어야 한다.

관측 대상에는 admission 결정, backend-call 여부, token ledger, MCP health/usage, Serena snapshot/consumption, gate 결과, transition receipt, violation, circuit breaker, migration manifest가 포함된다.

human viewer와 이 문서는 설명·관측 surface일 뿐 runtime input이 아니다. 큰 로그와 evidence bytes는 artifact store에 두고 SQLite에는 ref와 digest를 남긴다. 자세한 운영 검사는 [관측 워크플로](../workflow/15-observability-and-audit.md)를 참조한다.
