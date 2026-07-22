---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/capabilities.toml, ../../scripts/agent_team_integration.py]
---

# 통합과 release

PL은 TA가 승인한 exact OID만 integration worktree에 병합한다. merge conflict 또는 broken integration이면 failure/base OID와 evidence를 보존하고 새 rework를 배정한다. PL이 integration source를 직접 패치하지 않는다.

병합 성공 후 QA capability가 integration OID를 detached executable sandbox에서 기능·회귀·acceptance 기준으로 검증한다. QA가 실패하면 직접 수리하지 않고 PL로 반환한다. PL → DEV revision → TA review → PL remerge → QA revalidation 순서를 반복한다.

QA 승인 뒤 같은 QA+Build 물리 좌석은 기존 QA activation을 종료하고 새 `build_release` activation으로 build, package, install, upgrade, rollback을 검증한다. Build도 source repair나 QA 결과 수정 권한이 없다. 마지막 PM acceptance 역시 별도 capability activation이다.
