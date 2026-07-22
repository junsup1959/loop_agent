---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/mcp-policy.toml, ../../scripts/agent_team_taskflow.py]
---

# Plan IR과 Task DAG

PL은 승인된 목표를 bounded work item revision과 의존성으로 나눈다. 같은 repository의 독립 변경은 DEV_1·DEV_2 worktree에 병렬 배정할 수 있지만 동일 branch/worktree writer는 하나뿐이다.

계획 생성·수정 또는 rework 분해에는 `sequential-planning` 또는 `sequential-rework` binding이 적용된다. required usage receipt가 없는 계획은 admission/result validation을 통과하지 못한다.

Task DAG는 기존 TaskFlow 실행 구조로 컴파일된다. 이번 worktree 계층이 별도 상위 scheduler를 만들지 않는다. 재계획은 기존 revision을 덮어쓰지 않고 새 version과 evidence로 남긴다.
