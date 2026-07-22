---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../scripts/agent_team_recovery.py, ../../scripts/agent_team_contracts.py]
---

# 실패와 복구

복구 원칙은 “증거를 보존하고 알려진 base에서 재생성한 뒤 새 contract를 발급한다”이다.

| 실패 | 처리 |
|---|---|
| admission/MCP/digest/path 실패 | backend 0회, 원인 수정 후 새 admission |
| 순수 result format 실패 | output-only repair 1회 |
| 권한·OID·write scope·nested spawn 위반 | 즉시 quarantine |
| TA rejection | PL이 새 DEV rework 발급 |
| merge conflict/broken integration | OID 보존, integration 재생성, PL 재배정 |
| QA/Build 실패 | PL → DEV → TA → remerge → QA 반복 |
| dirty review source | evidence 무효, sandbox 재생성 |

reviewer와 PL의 direct source repair는 감사 chain을 깨므로 금지한다. 동일 worker의 반복 format violation은 circuit breaker 대상이다.
