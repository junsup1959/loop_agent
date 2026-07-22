---
runtime_injection: false
source_of_truth: [../../agents/ax-runtime.toml, ../../agents/team.toml, ../../agents/workflows/delivery-v4.toml]
---

# 아키텍처 문서 색인

이 디렉터리는 사람이 읽는 설명서다. 기존 Agent-Team 아키텍처를 대체하지 않으며, 독립 `AX_ROOT`, worktree, activation contract를 그 아키텍처의 실행 상세 계층으로 설명한다. 런타임은 이 문서 묶음을 주입하지 않는다.

| 문서 | 주제 |
|---|---|
| [00](00-system-context.md) | 시스템 경계 |
| [01](01-component-layers.md) | 구성요소 계층 |
| [02](02-project-layout.md) | 소스·런타임 배치 |
| [03](03-domain-and-state-model.md) | 도메인과 SQLite v4 |
| [04](04-team-and-authority-model.md) | 물리 좌석과 논리 권한 |
| [05](05-expertise-skill-system.md) | Skill·전문 프로파일 |
| [06](06-planning-and-orchestration.md) | 계획·오케스트레이션 |
| [07](07-messaging-and-state-store.md) | 메시지·상태 저장소 |
| [08](08-context-and-evidence-system.md) | 컨텍스트·Serena 증거 |
| [09](09-git-and-workspace-system.md) | Git·worktree 격리 |
| [10](10-agent-runtime-interfaces.md) | activation·runner 인터페이스 |
| [11](11-gate-and-evidence-model.md) | 검토·병합·QA 게이트 |
| [12](12-loop-control-model.md) | Goal·Module 루프 |
| [13](13-runtime-deployment.md) | 배포와 독립 AX |
| [14](14-security-and-authority-boundaries.md) | 보안·권한 경계 |
| [15](15-observability-data-model.md) | 관측·감사 |
| [16](16-large-scale-research-evidence.md) | 대규모 조사 증거 |

상태·권한·전이의 기계 권위는 TOML, JSON Schema, SQLite v4, `activation-contract.json`이다. 이 디렉터리는 해당 기계 권위를 설명하는 사람용 문서 계층이다.
