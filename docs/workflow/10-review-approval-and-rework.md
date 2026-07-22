---
runtime_injection: false
source_of_truth: [../../agents/workflows/delivery-v4.toml, ../../agents/capabilities.toml, ../../agents/mcp-policy.toml]
---

# 리뷰, 승인, rework

TA capability는 DEV가 제출한 exact OID를 detached executable sandbox에서 검토한다. source는 read-only지만 build/test output은 writable이므로 코드 실행과 검증이 가능하다. semantic review가 선언된 전이는 Serena tool usage receipt를 요구한다.

승인하면 PL에게 reviewed OID와 evidence를 보낸다. 거절하면 TA는 source를 수정하지 않고 finding, failure OID, 재현 evidence를 PL에게 보낸다.

PL은 `pl_issue_rework`로 새 DEV revision과 worktree 계약을 발급한다. 새 revision은 다시 TA exact-OID review를 통과해야 하며 이전 승인이나 profile을 재사용하지 않는다. PM+TA 좌석도 같은 activation에서 PM과 TA 권한을 동시에 행사하지 못한다.
