---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../agents/capabilities.toml, ../../agents/mcp-policy.toml]
---

# 보안과 권한 경계

권한은 physical seat, Skill, profile, model 또는 MCP availability가 아니라 admission된 단일 logical capability에서 나온다.

주요 fail-closed 경계는 다음과 같다.

- `AX_ROOT`, target checkout, canonical source의 중첩과 path escape 거부
- 개발자의 할당 worktree 밖 쓰기와 user checkout 쓰기 거부
- reviewer source read-only, exact OID, clean-source gate
- merge는 PL capability와 integration lease에만 허용
- elastic worker는 한 Goal/run당 하나, standing approval·merge·nested spawn 없음
- Serena·Sequential Thinking health/tool/use receipt 누락 시 no fallback 차단
- digest가 다른 contract/profile/Skill/template/result 거부

특권 migration·cutover·identity reset은 사람 승인과 별도 검증을 요구한다. 비밀이나 외부 access 권한은 Skill 문구로 부여할 수 없다.
