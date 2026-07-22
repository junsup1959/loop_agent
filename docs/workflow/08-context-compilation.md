---
runtime_injection: false
source_of_truth: [../../agents/context-profiles.toml, ../../agents/serena-knowledge-policy.toml, ../../scripts/serena_project_knowledge.py, ../../scripts/agent_team_context.py]
---

# 컨텍스트 컴파일

compiler는 activation contract, 같은 work item의 bounded message, base/head OID delta, artifact ref, 정확히 선택된 Skill, compiled profile을 예산 안에서 결합한다.

PL Serena onboarding이 필요한 assignment/rework에서는 다음을 먼저 확인한다.

- `initial_instructions` availability·invocation evidence
- new repository, missing memory, material change, stale knowledge trigger
- slow-changing content boundary와 policy/source digest
- transition별 최대 3개의 named memory ref/SHA-256

개발자는 source mutation 전에 선택 reference를 읽고 consumption receipt를 남긴다. active task/OID/approval/lease/team rule은 주입 계약·SQLite·Git·artifact에 남으며 shared memory로 옮기지 않는다.

예산 초과나 누락은 implicit preload가 아니라 exact missing ref를 담은 `NEED_MORE_CONTEXT`로 처리한다.
