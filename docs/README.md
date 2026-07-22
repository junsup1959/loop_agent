---
runtime_injection: false
source_of_truth: [../agents/team.toml, ../agents/workflows/delivery-v4.toml, ../agents/ax-runtime.toml]
---

# Agent-Team 문서

사람용 문서의 시작점이다. 이 문서들은 `runtime_injection: false`이며 agent activation에는 전체 문서 묶음 대신 deterministic contract와 선택 evidence만 들어간다.

- [아키텍처](architecture/INDEX.md): 기존 시스템 위의 독립 AX/worktree 상세 계층
- [워크플로](workflow/INDEX.md): PM → PL → DEV → TA → PL merge → QA → Build → PM 흐름
- [저장소 구성과 migration](operations/repository-configuration.md)
- [스크립트 운영](operations/scripts.md)
- [RTK](operations/rtk.md)

기계 권위:

- topology/capability: `agents/team.toml`, `agents/seat-slots.toml`, `agents/capabilities.toml`
- workflow/MCP/Serena: `agents/workflows/delivery-v4.toml`, `agents/mcp-policy.toml`, `agents/serena-knowledge-policy.toml`
- Skill/profile: `skills/catalog.toml`, `profile/catalog.toml`
- state/contracts: SQLite v4, `agents/contracts/schemas/`, `activation-contract.json`

최종 `.codex/`와 output bundle 상태는 manifest 기반 materialization 결과로 확인한다.
