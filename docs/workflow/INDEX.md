---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/workflows/catalog.toml, ../../agents/contracts/clause-catalog.toml]
---

# 워크플로 문서 색인

이 문서는 사람이 흐름을 이해하기 위한 색인이다. 상태·전이·clause·result kind는 연결된 TOML과 JSON Schema가 권위이며 런타임 agent에게 이 문서 전체를 읽히지 않는다.

| 문서 | 흐름 |
|---|---|
| [00](00-workflow-overview.md) | end-to-end delivery |
| [01](01-goal-intake.md) | 목표 접수 |
| [02](02-discovery-and-source-evidence.md) | 조사와 source evidence |
| [03](03-plan-ir-and-task-dag.md) | Plan IR·Task DAG |
| [04](04-role-and-skill-binding.md) | capability·Skill·profile binding |
| [05](05-taskflow-execution.md) | deterministic TaskFlow |
| [06](06-git-workspace-isolation.md) | branch·worktree·sandbox |
| [07](07-message-routing-and-agent-lifecycle.md) | 메시지·activation lifecycle |
| [08](08-context-compilation.md) | bounded context·Serena handoff |
| [09](09-agent-task-execution.md) | developer 실행 |
| [10](10-review-approval-and-rework.md) | TA review·rework |
| [11](11-module-development-loop.md) | module loop |
| [12](12-goal-supervisory-loop.md) | goal loop |
| [13](13-integration-and-release.md) | merge·QA·Build |
| [14](14-failure-and-recovery.md) | 실패·복구 |
| [15](15-observability-and-audit.md) | 관측·감사 |
| [16](16-large-scale-research-loop.md) | 대규모 조사 |
