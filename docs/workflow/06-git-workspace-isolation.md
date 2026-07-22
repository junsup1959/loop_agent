---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../scripts/agent_team_workspace.py, ../../scripts/agent_team_git.py]
---

# Git workspace 격리

PL이 source/base OID와 work item revision을 고정하면 workspace controller가 독립 `AX_ROOT`에 branch와 worktree lease를 만든다. DEV는 자신의 worktree만 writable root로 받는다.

TA review, QA와 Build 검증은 제출 또는 integration exact OID의 detached executable sandbox를 사용한다. source는 read-only이고 build/test/cache/temp/install만 writable이다. 실행 후 sandbox와 ephemeral root는 폐기한다.

cross-worktree path, user checkout write, duplicate writer, dirty reviewer source, OID drift는 fail closed다. cleanup은 active lease가 없고 owned manifest에 기록된 경로만 대상으로 한다.
