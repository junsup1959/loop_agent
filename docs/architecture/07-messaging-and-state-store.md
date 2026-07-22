---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_state.py, ../../scripts/agent_team_queue.py, ../../agents/team.toml]
---

# 메시징과 상태 저장소

SQLite v4가 제어 상태와 agent-to-agent message의 단일 영속 권위다. Git은 소스/OID, artifact store는 큰 증거, Serena memory는 느리게 변하는 프로젝트 지식을 담당한다.

메시지는 thread, work item, 발신/수신 capability, type, dedupe key, payload를 갖고 transactional outbox로 전달된다. 사람용 echo/viewer 출력은 관측 surface이며 다시 agent context로 주입하지 않는다.

activation admission, attempt, result, gate, MCP usage, Serena consumption, migration 기록은 immutable 또는 append-only 관계로 남는다. 실행기는 DB에서 확인하지 못한 권한을 메시지 텍스트로 부여하지 않는다.

운영 방법은 [scripts](../operations/scripts.md), context 선택 규칙은 [컨텍스트 시스템](08-context-and-evidence-system.md)을 참조한다.
