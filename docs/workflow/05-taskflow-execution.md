---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_taskflow.py, ../../scripts/agent_team_contracts.py, ../../agents/contracts/schemas/activation-result.schema.json]
---

# TaskFlow 실행

각 TaskFlow iteration은 다음 순서를 지킨다.

1. immutable DagRun input과 allocation/profile을 검증한다.
2. transition을 activation contract로 compile하고 admission한다.
3. bounded context artifact를 만든다.
4. developer라면 Serena consumption receipt를 source mutation 전에 materialize한다.
5. runner preflight 후 backend를 실행한다.
6. source integrity와 strict result schema를 검증한다.
7. MCP/Serena receipt를 기록하고 결과·메시지·전이를 영속화한다.
8. resource를 release하거나 quarantine한다.

admission, digest, path, OID, MCP preflight 실패는 backend call 0회다. stage 간 receipt가 없으면 다음 단계를 실행하지 않는다.
