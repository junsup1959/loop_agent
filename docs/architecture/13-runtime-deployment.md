---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../sample_config.toml, ../../scripts/init_agent_team.py, ../../scripts/build_agent_team_bundle.py]
---

# 런타임 배포

런타임은 대상 checkout 밖의 absolute `AX_ROOT`를 요구한다. 기본값은 플랫폼별 local application data 아래이며, 명시적 `--ax-root`로 재현할 수 있다. SQLite, managed repository, worktree, activation, artifact가 그 안에 배치된다.

Serena는 project cwd 기준 stdio server로, Sequential Thinking은 설치된 Node entrypoint로 구성한다. 두 MCP는 모두 `required = true`이고 health/tool preflight와 전이별 usage receipt가 필수다. shared HTTP Serena service나 optional/fallback 정책을 사용하지 않는다.

초기화·검증 예시는 [저장소 구성](../operations/repository-configuration.md)에 있다. canonical source가 구현 권위이며 `.codex/`와 `output/agent-team-codex-native/`가 이 설명과 최종 일치한다는 주장은 Phase 8 materialize/check가 끝난 뒤에만 가능하다.
