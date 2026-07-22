---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/ax-runtime.toml, ../../scripts/agent_team_taskflow.py]
---

# Agent task 실행

developer activation은 PL이 발급한 work item revision, exact base/subject OID, 전용 worktree lease, writable root, profile·Skill digest를 받는다. 다른 worktree와 사용자 checkout은 prohibited root다.

source를 처음 수정하기 전에 계약에 묶인 Serena named memory를 읽고 consumption receipt를 기록한다. `serena-coding-start`의 `initial_instructions` receipt도 필수이며 fallback이 없다.

개발자는 구현·테스트 후 새 commit OID와 artifact evidence를 제출한다. approval이나 merge는 할 수 없다. context가 부족하거나 root가 모호하면 임의 확대 대신 `NEED_MORE_CONTEXT`를 반환한다.
