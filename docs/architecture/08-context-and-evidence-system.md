---
runtime_injection: false
source_of_truth: [../../agents/context-profiles.toml, ../../agents/serena-knowledge-policy.toml, ../../scripts/agent_team_context.py]
---

# 컨텍스트와 증거 시스템

Context Compiler는 같은 Goal/work item과 대상 capability에 필요한 message, Git delta, artifact, profile, 선택 Skill만 예산 안에서 조립한다. 전체 저장소, 전체 thread, 전체 Skill catalog, 전체 Serena memory를 주입하지 않는다. 누락은 암묵적 확장이 아니라 `NEED_MORE_CONTEXT`로 요청한다.

Serena onboarding은 PL이 관리한다.

1. 새 저장소, 필수 memory 누락, 구조·설정의 material change, stale 지식을 판정한다.
2. `initial_instructions`와 onboarding 도구를 검증하고 필요한 slow-changing memory만 갱신한다.
3. `core`, `tech_stack`, `suggested_commands`, `conventions`, `task_completion` 중 transition에 필요한 최소 이름/ref/SHA-256만 snapshot에 고정한다.
4. 개발자는 source mutation 전에 해당 named reference를 읽고 consumption receipt를 남긴다.

허용 대상은 안정적 구조·소유권·관례·build/test 명령이다. active task, 현재 diff/OID, 승인, lease, team rule, prompt, per-run 결과는 SQLite·Git·artifact에 남긴다. 빠르게 변하는 코드 요약도 shared memory가 아니라 activation artifact다.

자세한 실행 흐름은 [컨텍스트 컴파일](../workflow/08-context-compilation.md)을 참조한다.
