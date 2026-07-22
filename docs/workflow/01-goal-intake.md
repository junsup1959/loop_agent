---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/capabilities.toml]
---

# 목표 접수

PM capability가 목표, 범위, 제약, acceptance criteria, 대상 repository를 명시한다. 한 Goal/run에 여러 repository가 필요한 요청은 repository별 Goal로 나눈다.

완료 조건은 검증 가능한 형태여야 하며 불명확한 항목은 `NEED_MORE_CONTEXT`로 남긴다. PM+TA 물리 좌석이 PM으로 활성화된 동안 TA gate 권한은 없다.

출력은 `pm_intake_goal` 전이의 schema-valid result와 durable evidence다. 승인된 목표는 PL assignment로 전달하며 메시지 문구가 전이 권한을 대신하지 않는다.
