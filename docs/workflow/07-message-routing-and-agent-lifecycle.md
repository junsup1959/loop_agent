---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_queue.py, ../../scripts/agent_team_state.py, ../../agents/team.toml]
---

# 메시지 라우팅과 activation lifecycle

구조화 메시지는 SQLite queue/outbox로 전달한다. thread, work item, from/to capability, message type, evidence ref, dedupe key가 필요하다. code와 큰 증거는 Git/artifact에 두고 메시지 payload에 복제하지 않는다.

activation lifecycle은 allocate → admit → attempt → run → result → release/quarantine 순서다. capability switch는 이전 lifecycle 종료 후 새 activation으로만 가능하다. elastic worker는 한 Goal/run당 하나이며 nested spawn을 할 수 없다.

viewer와 echo는 사람 관측 전용이다. 그 출력이나 전체 queue history를 자동으로 다음 agent context에 넣지 않는다.
