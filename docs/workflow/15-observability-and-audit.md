---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_state.py, ../../scripts/agent_team_message_viewer.py, ../../agents/contracts/schemas/contract-violation.schema.json]
---

# 관측과 감사

운영자는 SQLite와 artifact digest로 다음을 확인한다.

- contract가 legal transition, capability, lease, exact OID에 묶였는가
- admission 실패 때 backend call이 0회였는가
- profile·Skill·template·context digest가 일치하는가
- required MCP health/tool/use receipt와 Serena consumption이 존재하는가
- reviewer가 source를 바꾸지 않았고 gate가 동일 OID를 가리키는가
- 실패가 PL rework를 거쳤는가
- migration/deletion manifest와 rollback evidence가 보존됐는가

human viewer는 read-only 관측 도구다. 출력 전체를 agent context에 되먹이지 않는다. 필요한 evidence만 ref와 digest로 새 activation에 선택한다.
