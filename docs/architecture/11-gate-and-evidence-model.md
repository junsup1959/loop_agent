---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/capabilities.toml, ../../agents/contracts/clause-catalog.toml]
---

# 게이트와 증거 모델

기본 책임 흐름은 다음과 같다.

```text
PM requirement
→ PL assignment
→ DEV revision
→ TA exact-OID executable review
→ PL merge
→ QA post-merge validation
→ Build/release validation
→ PM acceptance
```

PM+TA와 QA+Build가 물리 좌석을 공유해도 각 gate는 별도 activation과 capability, profile, sandbox, OID evidence를 사용한다. 구현자는 자기 변경을 승인할 수 없다.

TA rejection, merge conflict, broken integration, QA/Build failure는 정확한 failure OID와 증거를 PL에게 돌려보낸다. PL은 새 DEV revision을 배정하고 TA 재검토 → PL 재병합 → QA 재검증을 반복한다. PL·QA·Build는 integration worktree에서 source를 직접 수리하지 않는다.

기계적 전이와 result kind는 prose가 아니라 [delivery-v4](../../agents/workflows/delivery-v4.toml)가 소유한다.
