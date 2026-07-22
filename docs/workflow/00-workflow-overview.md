---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/capabilities.toml]
---

# 워크플로 개요

```text
PM goal intake
→ PL assignment + Serena onboarding as required
→ DEV isolated worktree revision
→ TA exact-OID executable review
→ PL approved-OID merge
→ QA post-merge validation
→ Build/release validation
→ PM acceptance
```

한 Goal/run은 대상 저장소 하나를 사용하지만 두 developer가 서로 다른 branch/worktree에서 병렬 작업할 수 있다. 매 단계는 단일 active capability와 deterministic activation contract를 가진다.

TA rejection, merge conflict, broken integration, QA/Build failure는 PL rework로 돌아간다. PL은 failure OID를 보존하고 새 DEV revision을 배정하며 TA review → remerge → QA를 반복한다. QA·Build·PL은 실패 candidate source를 직접 고치지 않는다.

정확한 state와 transition은 [delivery-v4](../../agents/workflows/delivery-v4.toml)를 참조한다.
