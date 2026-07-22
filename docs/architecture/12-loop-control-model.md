---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../skills/goal-loop/SKILL.md, ../../skills/module-loop/SKILL.md]
---

# 루프 제어 모델

Goal loop는 목표·범위·완료 여부를 감독하고 Module loop는 bounded work item revision을 반복한다. 이번 worktree 설계는 두 루프의 기존 책임을 바꾸지 않는다.

각 반복은 immutable input과 새 activation contract를 가진다. 개발 성공은 새 Git OID로, 검토·QA 성공은 그 OID에 대한 receipt로 표현한다. 실패는 기존 activation을 다시 쓰지 않고 PL이 새 revision을 발급한다.

반복되는 동일 장애는 evidence와 시도 예산을 기준으로 block 또는 quarantine한다. 복잡한 재계획은 Sequential Thinking required-use binding을 거쳐야 한다. 상세 흐름은 [Module loop](../workflow/11-module-development-loop.md)와 [Goal loop](../workflow/12-goal-supervisory-loop.md)를 참조한다.
