---
runtime_injection: false
source_of_truth: [../../agents/seat-slots.toml, ../../agents/capabilities.toml, ../../skills/catalog.toml, ../../profile/catalog.toml]
---

# 역할과 Skill binding

배정 순서는 물리 slot → 단일 logical capability → runtime profile → Skill packet → compiled professional profile이다.

PM+TA와 QA+Build는 각각 하나의 물리 identity지만 activation마다 capability 하나만 선택한다. capability 교체는 새 activation이며 이전 권한·context·profile·sandbox를 재사용하지 않는다. 임시 Maestro 구현 agent 목록은 runtime topology가 아니다.

Skill은 catalog metadata와 package만으로 확장한다. exactly one `professional-profile-runtime`을 포함하고 상세 역할·gate·언어·toolchain reference는 `profile/`에서 digest-pinned compile한다. Skill/profile은 권한, 모델, MCP 범위, write scope, merge 권한을 넓히지 못한다.
