---
runtime_injection: false
source_of_truth: [agents/team.toml, agents/workflows/delivery-v4.toml, agents/ax-runtime.toml, docs/README.md]
---

# Agent-Team

기존 Agent-Team 아키텍처에 독립 `AX_ROOT`, Git worktree, exact-OID review sandbox와 deterministic activation contract를 더한 로컬 실행 제어면이다. 런타임 topology는 5개 고정 물리 좌석과 1개 탄력 슬롯이며, 병합 좌석은 activation마다 논리 capability 하나만 사용한다.

사람용 안내는 [docs/README.md](docs/README.md)에서 시작한다.

## 빠른 시작

일반 PowerShell에서 독립 런타임 경로를 지정한다.

```powershell
python .\scripts\init_agent_team.py --install-mcp-dependencies
python .\scripts\init_agent_team.py --ax-root C:\agent-team-ax\product
python .\scripts\init_agent_team.py --ax-root C:\agent-team-ax\product --check --json
python .\scripts\init_agent_team.py --check-mcp serena --check-mcp sequentialthinking --json
```

- [아키텍처](docs/architecture/INDEX.md)
- [워크플로](docs/workflow/INDEX.md)
- [구성·migration](docs/operations/repository-configuration.md)
- [스크립트](docs/operations/scripts.md)
- [RTK](docs/operations/rtk.md)

## 기계 권위

- topology·capability: `agents/team.toml`, `agents/seat-slots.toml`, `agents/capabilities.toml`
- workflow·MCP·Serena: `agents/workflows/delivery-v4.toml`, `agents/mcp-policy.toml`, `agents/serena-knowledge-policy.toml`
- Skill·profile: `skills/catalog.toml`, `profile/catalog.toml`
- contract·state: `agents/contracts/schemas/`, SQLite v4, `activation-contract.json`

이 README와 `docs/`는 `runtime_injection: false`다. 실제 `.codex/`와 `output/agent-team-codex-native/`의 최종 정합성은 Phase 8 materialize/check 결과로 확인한다.
