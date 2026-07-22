---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../sample_config.toml, ../../scripts/init_agent_team.py, ../../scripts/agent_team_migration.py, ../../scripts/build_agent_team_bundle.py]
---

# 저장소와 런타임 구성

## Canonical source와 실행 경계

`agents/`, `skills/`, `profile/`, `scripts/`가 편집 가능한 source다. 사람 문서는 `docs/`에 있고 runtime injection 대상이 아니다. topology·workflow·MCP·profile·Skill 권위는 각각의 TOML, JSON Schema, SQLite v4와 `activation-contract.json`에 있다.

대상 checkout 밖의 absolute `AX_ROOT`를 지정한다. 사용자 checkout은 read-only input이며 agent workspace나 state directory로 쓰지 않는다.

## 초기화와 검증

일반 PowerShell에서 실행한다.

```powershell
python .\scripts\init_agent_team.py --install-mcp-dependencies
python .\scripts\init_agent_team.py --ax-root C:\agent-team-ax\product
python .\scripts\init_agent_team.py --ax-root C:\agent-team-ax\product --check --json
python .\scripts\init_agent_team.py --check-mcp serena --check-mcp sequentialthinking --json
```

Serena는 stdio `start-mcp-server --project-from-cwd`, Sequential Thinking은 설치된 Node entrypoint를 사용한다. 둘 다 required이며 missing/unhealthy/tool 누락 시 fallback 없이 실패한다. 설정을 고친 뒤에는 `--refresh-mcp-config`와 `--check-mcp`를 실행한다.

## Legacy overlay migration

`dry-run`은 기본적으로 memory/stdout에 manifest를 만들며 쓰지 않는다. `--output`을 쓸 경우 source, target, legacy, AX root 밖의 안전한 파일을 지정한다.

```powershell
$common = @(
  '--target-checkout', 'C:\project\product',
  '--legacy-root', 'C:\project\product\.agent-team',
  '--ax-root', 'C:\agent-team-ax\product'
)
python -B .\scripts\agent_team_migration.py dry-run @common
python -B .\scripts\agent_team_migration.py apply @common
python -B .\scripts\agent_team_migration.py verify @common
python -B .\scripts\agent_team_migration.py cutover @common
python -B .\scripts\agent_team_migration.py rollback @common
```

권장 순서는 dry-run → manifest 검토 → apply → verify → cutover다. rollback은 atomic control pointer만 이전 상태로 복원하며 target checkout과 보존한 legacy evidence를 다시 쓰거나 제거하지 않는다. 애매한 legacy activation은 추측 변환하지 않고 quarantine해 PL 재발급 대상으로 남긴다.

## 생성물 정리

번들 빌더는 이전 owned manifest의 generated path만 제거한다. unknown 파일, 사용자 파일, database, artifact는 보존한다. 실제 `output/agent-team-codex-native/`와 `.codex/` 정합성은 Phase 8의 final materialize/check 전에는 완료로 주장하지 않는다.
