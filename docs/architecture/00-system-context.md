---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../agents/team.toml, ../../agents/mcp-policy.toml]
---

# 시스템 컨텍스트

Agent-Team은 한 Goal/run에서 하나의 대상 저장소를 다루되, 같은 저장소의 여러 branch와 worktree를 동시에 사용할 수 있는 로컬 제어면이다. 여러 저장소 변경은 별도 Goal로 나눠 복구·감사 경계를 명확히 한다.

독립 `AX_ROOT`는 사용자 checkout과 대상 저장소 밖에 위치한다. 이곳이 SQLite, 관리 저장소, worktree, activation, artifact를 소유하며 사용자 checkout은 에이전트 쓰기 대상이 아니다. 이 설계는 새로운 상위 오케스트레이터가 아니라 기존 Goal/Module/TaskFlow 아키텍처의 실행 격리 상세다.

외부 경계는 다음과 같다.

- 대상 Git 저장소: 소스와 immutable OID를 제공한다.
- Serena·Sequential Thinking: 필수 MCP 도구다. health/tool preflight와 전이별 사용 영수증이 없으면 fallback 없이 차단한다.
- 모델 backend: admission을 통과한 deterministic activation contract로만 호출한다.
- 사람 운영자: 목표 승인, 장애 해소, migration·cutover와 같은 특권 동작을 담당한다.

전체 흐름은 [워크플로 개요](../workflow/00-workflow-overview.md)를 참조한다.
