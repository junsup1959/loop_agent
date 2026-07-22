---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../scripts/agent_team_git.py, ../../scripts/agent_team_workspace.py]
---

# Git과 workspace 시스템

관리 repository와 모든 worktree는 독립 `AX_ROOT` 아래에 있다. 사용자 checkout과 관리 repository의 authoritative refs는 agent write 대상이 아니다.

DEV_1과 DEV_2는 같은 대상 저장소에서 서로 다른 branch/worktree lease를 동시에 가질 수 있다. `one active writer per branch/worktree`와 canonical path 검사가 교차 쓰기와 sibling worktree 접근을 막는다.

TA·QA·Build는 제출되거나 병합된 정확한 OID를 detached sandbox에서 검토한다. source는 read-only지만 build/test/cache/temp/install root는 쓰기 가능하므로 실제 실행 검증을 할 수 있다. 검토 중 source가 바뀌거나 dirty해지면 gate evidence는 무효다.

PL integration worktree만 승인된 OID 병합 권한을 가진다. 충돌이나 깨진 integration은 그 자리에서 고치지 않고 failure/base OID를 보존한 새 work item으로 되돌린다. 절차는 [Git 격리 워크플로](../workflow/06-git-workspace-isolation.md)를 참조한다.
