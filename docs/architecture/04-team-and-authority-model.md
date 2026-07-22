---
runtime_injection: false
source_of_truth: [../../agents/team.toml, ../../agents/seat-slots.toml, ../../agents/capabilities.toml]
---

# 팀과 권한 모델

런타임 topology는 고정 물리 좌석 5개와 탄력 슬롯 1개다.

| 물리 슬롯 | 가능한 논리 capability |
|---|---|
| PM+TA | `pm` 또는 `ta` |
| PL | `pl` |
| DEV_1 | `developer` |
| DEV_2 | `developer` |
| QA+Build | `qa_sdet` 또는 `build_release` |
| Elastic | `worker` 또는 `advisory` |

PM+TA와 QA+Build는 물리 identity만 공유한다. 한 activation에서는 capability 하나만 활성화되며 비활성 capability의 권한, profile, 도구, 컨텍스트를 상속하지 않는다. 전환 때 기존 결과를 확정하고 profile·sandbox·lease를 폐기한다.

PL만 배정, 승인된 OID 병합, 실패 재분배를 담당한다. TA는 exact-OID 아키텍처·코드 리뷰, QA는 병합 후 품질 검증, Build는 후속 build/release 검증을 수행한다. 어느 reviewer도 소스를 직접 고치지 않는다.

여기서 말하는 물리 topology는 이번 구현에 잠시 사용한 Maestro agent 목록과 무관하다. 실제 권한은 오직 admission된 logical capability에서 나온다.
