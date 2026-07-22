---
runtime_injection: false
source_of_truth: [../../skills/module-loop/SKILL.md, ../../agents/workflows/delivery-v4.toml]
---

# Module development loop

Module loop는 하나의 bounded work item을 revision 단위로 진행한다.

```text
PL assignment → DEV commit → TA review → PL merge → QA validation
          ↑             failure evidence             |
          └──────────── PL rework ───────────────────┘
```

각 반복은 새 contract, lease, exact OID, profile과 receipt를 가진다. 같은 실패 candidate를 review/integration sandbox에서 수정하지 않는다. 반복 예산을 초과하거나 복구 근거가 없으면 상위 Goal loop에 block/replan evidence를 전달한다.
