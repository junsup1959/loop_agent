---
runtime_injection: false
source_of_truth: [../../agents/contracts/schemas/activation-contract.schema.json, ../../agents/templates/activation-packet.md.tmpl, ../../scripts/agent_team_contracts.py, ../../scripts/agent_team_runtime.py]
---

# Agent runtime 인터페이스

`activation-contract.json`이 실행 권위다. Markdown packet은 같은 계약과 versioned clause/template에서 만든 deterministic view이며 독립 권위가 아니다.

계약에는 workflow/transition, 물리 slot, 활성 capability, worker fingerprint, exact OID, lease와 path scope, profile, Skill, MCP binding, Serena references, output schema, budget, idempotency key와 digest가 들어간다. Context Compiler는 이를 bounded evidence와 결합한다.

Runner는 backend 호출 전에 계약·packet·profile·context·environment digest와 sandbox binding을 다시 검증한다. 실패 시 backend call은 0회다. 결과는 strict JSON Schema를 만족하고 필요한 MCP usage 및 Serena consumption receipt를 포함해야 한다.

순수 출력 형식 오류는 한 번의 저비용 output-only repair 대상이 될 수 있다. 권한·OID·write scope·nested spawn 위반은 즉시 quarantine한다.
