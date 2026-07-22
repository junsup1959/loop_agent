---
runtime_injection: false
source_of_truth: [../../agents/team.toml, ../../agents/ax-runtime.toml, ../../scripts/agent_team_contracts.py]
---

# 구성요소 계층

의존 방향은 사람이 읽는 설명에서 기계 권위로, 그리고 실행으로 내려간다.

1. `docs/`: 설명과 운영 안내. `runtime_injection: false`다.
2. `agents/*.toml`, `skills/catalog.toml`, `profile/catalog.toml`, JSON Schema: topology, capability, workflow, MCP, Skill, profile 계약.
3. SQLite v4: Goal/run, lease, activation, receipt, 위반과 전이를 정규화해 저장한다.
4. 계약 계층: 전이를 컴파일하고 JSON 계약과 Markdown packet을 결정적으로 렌더링한다.
5. 실행 계층: 독립 worktree 또는 exact-OID sandbox를 만들고 runner를 제한한다.
6. 증거 계층: Git OID, artifact, MCP/Serena receipt, gate 결정을 append-only로 연결한다.

문서는 계약 내용을 통째로 복제하지 않는다. 실제 전이와 clause는 [delivery-v4](../../agents/workflows/delivery-v4.toml)와 계약 카탈로그가 소유한다.
