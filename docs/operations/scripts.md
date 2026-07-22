---
runtime_injection: false
source_of_truth: [../../scripts/init_agent_team.py, ../../scripts/project_agents.py, ../../scripts/project_skills.py, ../../scripts/agent_team_migration.py, ../../scripts/build_agent_team_bundle.py]
---

# 스크립트 운영 표면

| 스크립트 | 책임 |
|---|---|
| `init_agent_team.py` | 독립 AX 초기화, MCP 설치·검사, non-mutating check |
| `project_agents.py` | 5 fixed + 1 elastic topology 검증·해결 |
| `project_skills.py` | catalog 검증과 digest-pinned Skill packet |
| `agent_team_state.py` | strict SQLite v4 |
| `agent_team_contracts.py` | compile/admit/render/result control |
| `agent_team_workspace.py` | developer worktree lease |
| `agent_team_review.py` | exact-OID executable sandbox |
| `agent_team_taskflow.py` | 기존 TaskFlow와 activation gate 연결 |
| `serena_project_knowledge.py` | PL onboarding snapshot·memory selection |
| `agent_team_migration.py` | dry-run/apply/verify/cutover/rollback |
| `build_agent_team_bundle.py` | deterministic materialize/check와 owned cleanup |

주요 read/validate 명령:

```powershell
python -B .\scripts\project_agents.py validate
python -B .\scripts\project_skills.py validate
python -B .\scripts\init_agent_team.py --check --json
python -B .\scripts\build_agent_team_bundle.py --check --destination C:\temp\agent-team-bundle
```

개발 중에는 disposable destination에만 bundle을 materialize한다. 빌더는 unsafe/unowned destination을 거부하고 unknown 파일을 보존한다. Phase 8 release 전에는 live `.codex/`나 real output이 canonical source와 이미 동기화됐다고 가정하지 않는다.

과거 `scripts/README.md`의 8-seat, optional MCP, shared Serena HTTP 설명은 현재 계약이 아니다. CLI `--help`와 위 source 파일을 우선한다.
