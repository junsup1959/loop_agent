---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../scripts/agent_team_layout.py, ../../scripts/build_agent_team_bundle.py]
---

# 프로젝트 배치

편집 가능한 canonical source는 저장소 루트의 `agents/`, `skills/`, `profile/`, `scripts/`다. `docs/`는 사람 문서이며 `output/agent-team-codex-native/`는 생성물이다.

독립 런타임은 대상 checkout 밖의 `AX_ROOT`를 사용한다.

```text
AX_ROOT/
  state/agent-team.db
  repositories/
  workspaces/<goal_id>/<run_id>/<lease_id>/
  activations/
  artifacts/
```

`AX_ROOT`, canonical source, 대상 checkout은 서로 겹치면 안 된다. 개발자는 할당된 worktree만 수정하고, 리뷰어는 source-read-only sandbox와 별도 build/test/cache/temp/install 경로만 쓴다.

번들 빌더는 이전 owned manifest에 있던 생성 경로만 정리하며 unknown 파일은 보존한다. 실제 `.codex/`와 `output/`의 최종 동기화는 Phase 8 materialization 이후에만 완료로 간주한다. 운영 명령은 [저장소 구성](../operations/repository-configuration.md)을 참조한다.
