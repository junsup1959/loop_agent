---
runtime_injection: false
source_of_truth: [../../skills/catalog.toml, ../../profile/catalog.toml, ../../scripts/project_skills.py, ../../scripts/agent_team_profiles.py]
---

# Skill과 전문 프로파일

Skill은 package와 `skills/catalog.toml` metadata로 확장한다. 새 package 경로, version, SHA-256, kind, eligible capability, 문자 예산, MCP prerequisite를 catalog revision에 추가하면 되며 core Python의 하드코딩 목록을 수정하지 않는다.

resolver는 활성 capability와 transition이 이미 허용한 MCP 범위 안에서만 prerequisite를 인정한다. Skill은 권한, 모델, write scope, gate, merge 권한을 확장할 수 없다. 활성 계약은 선택 당시 bytes와 digest에 고정된다.

모든 activation은 정확히 하나의 `professional-profile-runtime` Skill을 가진다. 언어·역할·gate·toolchain 상세는 별도 `profile/` reference에서 선택·컴파일하고 digest로 묶는다. capability가 끝나면 profile도 폐기한다.

검증·해결 명령은 [scripts 운영](../operations/scripts.md)을 참조한다.
