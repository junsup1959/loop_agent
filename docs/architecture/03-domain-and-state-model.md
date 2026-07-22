---
runtime_injection: false
source_of_truth: [../../scripts/agent_team_state.py, ../../scripts/agent_team_domain.py, ../../agents/workflows/delivery-v4.toml]
---

# 도메인과 상태 모델

핵심 계층은 Target → Goal → Run → Workflow instance → Activation/Attempt다. 한 Goal/run은 하나의 repository registration에 묶이지만 여러 work item, branch, worktree와 exact OID를 가질 수 있다.

SQLite schema v4는 다음 관계를 정규화한다.

- 물리 seat, 논리 capability, slot, worker fingerprint
- workflow definition/state/transition/instance
- repository, workspace lease, sandbox, OID authority
- activation contract, clause, admission, attempt, result, violation, circuit breaker
- profile·Skill·MCP binding과 health/usage receipt
- Serena snapshot, 선택 memory binding, consumption receipt
- migration/deletion manifest와 보존 증거

foreign key, CHECK, UNIQUE, partial UNIQUE, immutable trigger가 잘못된 조합을 차단한다. WAL, foreign key, `BEGIN IMMEDIATE` 소유권 트랜잭션이 기본이며, 상태를 문서나 파일명에서 추론하지 않는다.
