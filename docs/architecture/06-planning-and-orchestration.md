---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../scripts/agent_team_taskflow.py, ../../agents/mcp-policy.toml]
---

# 계획과 오케스트레이션

기존 Goal/Module/TaskFlow 구조가 상위 제어를 유지한다. 이번 설계는 그 위에 새 오케스트레이터를 만들지 않고 각 전이를 독립 worktree·activation contract로 실행 가능하게 한다.

PM은 목표·범위·acceptance를 확정하고 PL은 work item과 의존성을 배정한다. 복잡한 계획·재계획 전이는 Sequential Thinking 사용과 receipt를 요구한다. transition compiler는 상태, capability, OID, lease, profile, Skill, MCP, Serena reference, clause, 결과 schema를 한 계약으로 고정한다.

admission 실패는 model call 0회다. 성공한 계약만 Context Compiler → Runner → result validator → 상태 전이로 이동한다. 상세 순서는 [TaskFlow 실행](../workflow/05-taskflow-execution.md)에 있다.
