---
runtime_injection: false
source_of_truth: [../../skills/goal-loop/SKILL.md, ../../agents/workflows/delivery-v4.toml, ../../agents/mcp-policy.toml]
---

# Goal supervisory loop

Goal loop는 PM의 acceptance와 PL의 work item 상태를 종합해 다음 revision, replan, block, completion을 결정한다. 한 Goal/run은 repository 하나에 묶인다.

독립 work item은 두 developer worktree로 병렬화할 수 있지만 gate 순서는 OID별로 유지한다. 요구사항 변경이나 반복 실패는 새 plan revision으로 표현하며 복잡한 재계획은 Sequential Thinking required-use receipt를 남긴다.

모든 필요한 QA/Build/PM gate가 동일 integration OID를 승인하기 전에는 completed로 전이하지 않는다. 문서의 완료 선언이 SQLite workflow state를 대신하지 않는다.
