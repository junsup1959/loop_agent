# 로컬 자율 개발 Agent Team 설계

## 1. 문서 정보

| 항목 | 내용 |
|---|---|
| 문서 상태 | Draft |
| 대상 환경 | 로컬 Windows, 오프라인·데스크톱·솔루션 개발 |
| 대상 규모 | 약 10만 줄 이상의 다중 프로젝트 코드베이스 |
| 원격 저장소 | 사용하지 않음 |
| 실행 오케스트레이터 | Apache Airflow TaskFlow API |
| 계획·추론 | Sequential Thinking MCP |
| 소스 코드 읽기·구조 분석 | Serena MCP |
| 메시지·업무 상태 | SQLite |
| 코드·변경 증거 | Local Git |

## 2. 목적

이 시스템의 목적은 단순히 여러 Agent에게 프롬프트를 병렬로 전달하는 것이 아니다.

최상위 Goal이 주어지면 다음 개발 활동을 역할과 책임에 따라 자동으로 반복하여, 사전에 정의된 권한과 승인 정책 안에서 사람의 지속적인 개입 없이 개발을 진행하는 로컬 자율 개발팀을 구성한다.

1. 요구사항 정리와 완료 조건 확정
2. 저장소 및 솔루션 구조 조사
3. 작업 분해와 의존성 추론
4. 역할 및 Skill 배정
5. 모듈별 설계와 구현
6. 빌드·테스트·코드 검토
7. 승인 실패에 따른 재작업과 재검토
8. 통합 및 Goal 완료 판정

시스템은 사람 없이 무조건 결과를 만드는 것을 목표로 하지 않는다. 승인된 범위에서는 자율적으로 작업하고, 근거 부족·권한 초과·반복 실패·정책 위반이 발생하면 안전하게 `BLOCKED` 상태로 종료할 수 있어야 한다.

## 3. 범위

### 3.1 포함 범위

- 최대 8개 Agent 슬롯으로 구성되는 개발팀
- 조직 직책과 기술 전문성의 분리
- Skill의 작업별 동적 장착
- Sequential Thinking MCP를 이용한 작업 분해·의존성 추론·대안 비교
- Serena MCP를 이용한 소스 코드 읽기·구조·심볼·참조 분석
- Plan IR 생성·검증 및 TaskFlow DAG 변환
- SQLite 기반 역할 간 메시지 큐와 상태 관리
- 로컬 Hook·Dispatcher 기반 Agent 활성화
- 역할별 Context Snapshot 생성
- Local Git commit OID 기반 변경 추적
- 작업·revision별 독립 branch와 worktree
- 검토·승인·수정·재검토 프로토콜
- Module Development Loop
- Goal Supervisory Loop

### 3.2 제외 범위

- 원격 Git 서버 및 외부 Artifact Storage
- Agent 간 무제한 자유 대화
- 여러 Agent가 동일 workspace 또는 동일 branch를 동시에 수정하는 방식
- 실행 중인 하나의 Airflow DAG topology를 임의로 순환 변경하는 방식
- 구현 Agent의 자기 승인
- LLM의 자연어 판단만으로 이루어지는 최종 통합

## 4. 설계 원칙

### 4.1 역할과 전문성 분리

Agent의 조직적 책임과 승인 권한은 역할로 유지한다. 언어·프레임워크·플랫폼·도메인 전문성은 Skill로 작업마다 동적으로 부여한다.

```text
역할
= 누가 책임지고 누구에게 승인받는가

Skill
= 이번 작업을 수행하기 위해 어떤 전문성이 필요한가
```

### 4.2 실행과 기억 분리

Agent 프로세스는 상주 인력이 아니다. Agent는 Task 또는 메시지가 도착할 때 활성화되고, 역할·Skill·업무 상태·관련 증거를 복원한 뒤 결과를 남기고 종료한다.

지속성은 Agent 프로세스가 아니라 다음 저장 계층이 담당한다.

- SQLite: Goal, Work Item, 메시지, 결정, finding, Loop 상태
- Git: 코드 상태, commit, branch, diff
- Local Artifact Store: 빌드·테스트·분석 결과
- Context Snapshot: 역할별 압축 문맥

### 4.3 메시지와 컨텍스트 분리

SQLite 큐에 메시지가 많이 저장되는 것과 모델 컨텍스트가 커지는 것은 별개다. Agent에게 큐 전체를 전달하지 않는다.

```text
Raw Message
→ Thread State Projection
→ Role Context Snapshot
→ Agent Context
```

### 4.4 코드와 업무 상태 분리

```text
Local Git
= 코드와 변경 증거의 기준

SQLite
= 대화, 책임, 승인, 상태의 기준
```

SQLite 메시지에는 코드 본문을 저장하지 않고 Git OID와 Artifact 참조를 저장한다.

### 4.5 재시도와 재작업 분리

```text
Retry
= 동일 입력의 인프라·프로세스 실패 재실행

Rework
= 검토 또는 테스트 지적을 반영한 새 revision
```

코드 검토 실패를 Airflow Task 실패로 표현하지 않는다.

### 4.6 증거 기반 승인

승인은 요약문이 아니라 정확한 commit, 테스트 결과, 검토 finding과 연결되어야 한다.

```text
approved_oid == tested_oid == integration_target_oid
```

세 값이 다르면 해당 Gate는 무효화한다.

## 5. 전체 아키텍처

```text
                             ┌──────────────────────┐
                             │        Goal          │
                             │ 목표·제약·완료 조건  │
                             └──────────┬───────────┘
                                        │
                             ┌──────────▼───────────┐
                             │ Goal Supervisory Loop│
                             └──────────┬───────────┘
                                        │
                         Sequential Thinking MCP
                       작업 분해·의존성·대안 비교
                                        │
                             ┌──────────▼───────────┐
                             │       Plan IR        │
                             │ Task·Role·Skill·Gate │
                             └──────────┬───────────┘
                                        │ 검증·컴파일
                             ┌──────────▼───────────┐
                             │ Airflow TaskFlow API │
                             └──────────┬───────────┘
                                        │
               ┌────────────────────────┼────────────────────────┐
               ▼                        ▼                        ▼
       SQLite Message Queue       Context Compiler         Local Git
       Thread·Decision·Finding    역할별 문맥 구성       Commit·Diff·Worktree
               │                        │                        │
               └──────────────┬─────────┴─────────┬──────────────┘
                              ▼                   ▼
                        Role Agent Task     Artifact/Evidence
                              │
                    구현·검토·승인·재작업
                              │
                              ▼
                     Module Development Loop
                              │
                              └──── 결과를 Goal Loop로 환류
```

## 6. Agent 팀 단위 구성

### 6.1 기본 8개 슬롯

| Agent ID | 조직 역할 | 주요 책임 | 주요 권한 |
|---|---|---|---|
| `pm` | PM | Goal 해석, 범위, 우선순위, 완료 조건 | 요구사항·범위 승인 |
| `pl` | PL/Tech Lead | 작업 분해·배정, 의존성, 소유권, 통합 | Plan·통합 최종 승인 |
| `ta` | Technical Architect | 솔루션 구조, 인터페이스, 기술 위험, ADR | 아키텍처 승인·수정 요청 |
| `dev_1` | Senior Developer Slot | 배정된 모듈 조사·구현·단위 테스트 | 자신의 작업 branch 수정 |
| `dev_2` | Senior Developer Slot | 배정된 모듈 조사·구현·단위 테스트 | 자신의 작업 branch 수정 |
| `dev_3` | Senior Developer Slot | 배정된 모듈 조사·구현·단위 테스트 | 자신의 작업 branch 수정 |
| `qa_sdet` | QA/SDET | 테스트 설계·자동화·회귀·복구 검증 | 품질 Gate 판정 |
| `build_release` | Build/Release·CM | 빌드·의존성·설치·업데이트·롤백 | Release Gate 판정 |

최대 8개는 동시에 존재하는 논리적 슬롯의 상한이다. 모든 슬롯을 항상 실행 상태로 둘 필요는 없다.

### 6.2 Developer Skill 동적 배정

Developer 슬롯의 전문성은 영구 직책이 아니라 작업별 Skill로 결정한다.

예:

```text
dev_1 + cpp-concurrency + service-lifecycle
dev_2 + desktop-ui + win32-integration
dev_3 + sqlite-storage + backward-compatibility
```

다음 iteration에서는 동일한 슬롯에 다른 Skill을 장착할 수 있다.

```text
dev_1 + parser-design
dev_2 + build-system
dev_3 + performance-profiling
```

### 6.3 Agent 활성화 생명주기

```text
IDLE
→ 메시지 또는 Task 배정
→ 역할·Skill·Context Snapshot 로드
→ 독립 workspace 할당
→ 작업 수행
→ 결과·증거·메시지 저장
→ ACK
→ IDLE 또는 종료
```

Agent ID는 지속적인 역할 정체성을 의미하지만 실행 프로세스의 상주를 의미하지 않는다.

### 6.4 책임 분리

- PM은 코드를 직접 승인하지 않는다.
- 구현자는 자신의 변경을 최종 승인하지 않는다.
- TA는 아키텍처를 검토하지만 QA 통과를 대체하지 않는다.
- QA는 요구사항이나 아키텍처를 임의로 변경하지 않는다.
- Build/Release는 테스트를 생략하고 배포 승인을 내릴 수 없다.
- PL은 모든 증거 Gate가 같은 commit을 대상으로 하는지 확인한 후 통합한다.

## 7. MCP 계획 계층

### 7.1 Trigger 우선순위

프로젝트 `AGENTS.md`의 규칙을 계획 계층의 진입 조건으로 사용한다.

1. If a Serena source-exploration trigger applies, collect the required targeted source evidence first.
2. After source evidence is available, invoke Sequential Thinking when decomposition, dependency reasoning, or alternative comparison is required.

### 7.2 Serena MCP 단독 Trigger

- 분석해야 할 소스 코드가 50K token을 초과
- 특정 함수·클래스 변경의 참조 및 영향도 역추적
- 여러 파일에 걸친 구조·심볼·아키텍처 의존성 분석

All roles may use Serena for targeted source exploration, including symbol and reference lookup, structural and dependency analysis, impact evidence, and the project-memory references selected for their activation. Tool availability does not grant planning, work allocation, approval, workspace, write-scope, or release authority.

The PL alone publishes, refreshes, renames, or deletes shared Serena project memory. Other roles submit concise evidence-backed proposals through SQLite. Serena memory contains slow-changing project knowledge only; task state, approvals, test results, branches, Git OIDs, and agent messages remain in SQLite, Git, or local artifacts.

The project starts Serena through `$agent-team-bootstrap`, which invokes `$serena-project-setup` before `init_agent_team.py`. `$serena-project-setup` creates or repairs the Serena project, indexes and health-checks it, initializes memories, and starts one shared loopback Streamable HTTP service on a random persisted port. Every spawned agent connects to that endpoint; no per-agent Serena stdio process is started.

Serena-derived evidence is pinned to repository and commit OID before planning. Planning, alternative comparison, and final technical judgment remain separate Sequential Thinking and role-accountability steps.

```text
Repository Map
Symbol Map
Reference Graph
Dependency Evidence
Impact Candidates
```

### 7.3 Sequential Thinking MCP Trigger

- 앞선 결과가 뒤의 결정에 영향을 주는 다단계 추론
- 비용·성능·보안·호환성 등 상반된 제약의 비교
- 대형 코드베이스에서 변경과 영향 범위를 함께 계획
- Work Item 분해와 의존성 추론
- 대안 비교와 실패 경로 정의
- Goal 또는 Module Loop 재계획

### 7.4 MCP 배치 방식

Sequential Thinking MCP는 프로젝트 로컬 npm 설치 또는 Docker MCP를 통해 제공할 수 있다. 두 배치는 동일한 논리적 `Planning Provider` 인터페이스로 취급한다.

한 Plan 생성에서 npm 인스턴스와 Docker 인스턴스를 동시에 호출하지 않는다. 프로젝트 구성에서 하나를 활성 Provider로 선택하고 다른 하나는 대체 실행 경로로만 사용한다.

### 7.5 Plan IR

Sequential Thinking의 최종 출력은 자유 형식 설명이 아니라 검증 가능한 Plan IR이어야 한다.

```yaml
plan_id: PLAN-G001-R3
goal_id: G-001
revision: 3
assumptions:
  - id: A-01
    text: StateManager의 공개 계약은 유지한다.
constraints:
  - id: C-01
    text: 원격 저장소를 사용하지 않는다.
work_items:
  - id: W-42
    objective: 종료 중 callback 재진입 방지
    owner_role: developer
    required_skills:
      - cpp-concurrency
      - service-lifecycle
    dependencies:
      - W-31
    read_scope:
      - src/runtime/**
      - src/platform/**
    write_scope:
      - src/runtime/**
      - tests/runtime/**
    required_context:
      - REQ-12
      - ADR-17
      - symbol:StateManager
    output_contract:
      - implementation_commit
      - unit_test_result
      - risk_report
    review_gates:
      - architecture_review
      - code_review
      - regression_test
    failure_routes:
      context_missing: DISCOVER
      design_rejected: DESIGN
      test_failed: IMPLEMENT
    budget:
      max_iterations: 4
      token_budget: configured
```

### 7.6 Plan 검증

TaskFlow 실행 전에 다음을 검사한다.

- Work Item ID 중복
- 의존성 순환
- 존재하지 않는 선행 작업
- write scope 중복
- 역할과 승인자 충돌
- 자기 승인 경로
- 필요한 Skill 누락
- 완료 증거 계약 누락
- 실패 경로 누락
- 반복 횟수와 예산 누락
- 권한 밖 파일 변경

검증에 실패한 Plan은 실행하지 않고 Sequential Thinking 재계획 입력으로 반환한다.

## 8. Airflow TaskFlow API 실행 계층

### 8.1 역할

Airflow는 다음을 책임진다.

- Plan IR의 실행 순서
- 선행 작업 완료 확인
- 독립 Work Item의 병렬 실행
- Agent Task 활성화
- 인프라 실패 재시도
- 메시지와 Artifact 전달
- 검토 Gate 호출
- iteration과 DagRun 기록

Airflow는 다음을 책임지지 않는다.

- 코드 의미 분석
- 최종 기술 판단
- 역할별 Context 압축
- 실행 중 임의의 DAG 순환 변경

### 8.2 Plan IR에서 TaskFlow로 변환

```text
Sequential Thinking
→ Plan IR
→ Plan Validator
→ Dependency Topological Sort
→ TaskFlow DAG Compiler
→ DagRun
```

임의 topology는 DagRun 전에 확정한다. 실행 중 계획 변경이 필요한 경우 현재 iteration을 종료하고 새 Plan revision과 새 DagRun을 생성한다.

### 8.3 TaskFlow 개념 구조

```python
@dag
def module_iteration():
    plan = load_validated_plan()
    context = build_context(plan)
    implementation = run_agent(context)
    evidence = verify_implementation(implementation)
    review = review_change(evidence)
    decision = evaluate_gate(review)
    persist_iteration_result(decision)
```

실제 구현에서는 역할·Work Item·revision을 매개변수로 전달하고, 독립 작업은 Dynamic Task Mapping 또는 컴파일된 Task dependency로 실행한다.

### 8.4 권장 DAG 단위

| DAG | 책임 |
|---|---|
| `goal_supervisor_dag` | Goal 상태 확인, Plan 생성·재계획, Workstream 조정 |
| `module_iteration_dag` | 한 Module의 한 iteration 실행 |
| `review_gate_dag` | 역할별 검토·승인·재검 요청 |
| `integration_dag` | 승인된 commit 통합 및 시스템 검증 |
| `release_dag` | 로컬 설치·업데이트·롤백 검증 |

### 8.5 Task 결과 계약

Task 출력에 코드 전체나 긴 로그를 넣지 않는다.

```json
{
  "work_item_id": "W-42",
  "status": "IMPLEMENTED",
  "repo_id": "product",
  "base_oid": "71ae234f9c...",
  "head_oid": "d920f31a82...",
  "artifact_refs": [
    "artifact://tests/W-42/r3/result.json"
  ],
  "message_ids": [
    "msg-1024"
  ]
}
```

## 9. 업무 객체 계층

```text
Goal
└─ Workstream
   └─ Module Loop
      └─ Work Item
         └─ Task/Agent Turn
            └─ Artifact/Evidence
```

### 9.1 Goal

장기간 유지되는 최상위 목표다. 여러 DagRun과 Agent 생명주기를 초월한다.

```text
Goal
├─ objective
├─ success criteria
├─ constraints
├─ authority boundary
├─ active workstreams
├─ completed evidence
├─ unresolved risks
├─ budget
└─ status
```

### 9.2 Workstream

독립적으로 관리 가능한 개발 영역이다.

예:

- solution-build
- parser-core
- local-storage
- desktop-platform
- quality-release

### 9.3 Work Item

한 역할이 책임지고 검토 가능한 단위로 완료할 수 있는 작업이다.

필수 속성:

- 명확한 목표
- 담당 역할
- 필요한 Skill
- 선행 작업
- 읽기·쓰기 범위
- 입력 Context 계약
- 출력 Artifact 계약
- 검토자
- 완료 조건
- 실패 경로

## 10. SQLite 메시지 및 상태 계층

### 10.1 목적

SQLite는 역할 간 업무 메시지, 현재 상태, 검토 결정, 미해결 finding과 Agent 활성화 이벤트를 관리한다.

SQLite는 코드와 대형 로그 저장소가 아니다.

### 10.2 주요 테이블

```text
goals
workstreams
work_items
loop_iterations
threads
messages
message_deliveries
agent_cursors
decisions
findings
thread_snapshots
outbox
workspace_leases
artifact_refs
```

### 10.3 Message Envelope

```json
{
  "id": "msg-1024",
  "thread_id": "thread-W42",
  "work_item_id": "W-42",
  "parent_message_id": "msg-1008",
  "from_role": "dev_1",
  "to_role": "ta",
  "type": "REVIEW_REQUEST",
  "priority": 50,
  "payload": {
    "repo_id": "product",
    "review_type": "architecture",
    "base_oid": "71ae234f9c...",
    "head_oid": "d920f31a82...",
    "context_profile": "architecture-review"
  },
  "dedupe_key": "W-42:architecture:d920f31a82",
  "created_at": "timestamp"
}
```

### 10.4 메시지 유형

```text
ASSIGN
ACCEPT
QUESTION
ANSWER
BLOCKED
CONTEXT_REQUIRED
CONTEXT_ADDED
REVIEW_REQUEST
APPROVED
CHANGES_REQUESTED
REJECTED
REWORK_SUBMITTED
TEST_FAILED
TEST_PASSED
CONTRACT_CHANGE_PROPOSED
CONTRACT_CHANGED
ESCALATE
CLOSE
```

### 10.5 전달 상태

```text
PENDING
→ CLAIMED
→ RUNNING
├─ ACKED
├─ RETRY
└─ DEAD_LETTER
```

`lease_until`을 이용하여 Agent 실행이 중단된 메시지를 다시 전달할 수 있어야 한다.

전달 보장은 다음 조합을 사용한다.

```text
at-least-once delivery
+ dedupe key
+ idempotent handler
```

### 10.6 Transactional Outbox

메시지 저장과 알림 발행 사이의 손실을 막기 위해 동일 transaction에 `messages`와 `outbox`를 기록한다.

```text
BEGIN
→ messages INSERT
→ outbox INSERT
→ COMMIT
→ local dispatcher wake-up
```

### 10.7 Hook과 Dispatcher

SQLite 내부 update hook은 다른 프로세스의 변경을 신뢰성 있게 알리는 프로세스 간 메시지 버스가 아니다.

따라서 알림은 애플리케이션 큐 계층에서 commit 이후 발생시킨다.

권장 구성:

```text
SQLite              영속 메시지
Local Dispatcher    라우팅과 batch
Windows Named Pipe  즉시 wake-up
Periodic Polling    알림 누락 복구
```

알림은 최적화이며 SQLite가 진실의 원천이다.

### 10.8 메시지 Batch

메시지 하나마다 Agent를 호출하지 않는다.

```text
batch key
=(work_item_id, thread_id, to_role)
```

다음 메시지는 즉시 또는 높은 우선순위로 처리한다.

- `BLOCKED`
- `CHANGES_REQUESTED`
- `CONTRACT_CHANGED`
- `CRITICAL_FINDING`
- `GOAL_BLOCKED`

다음 메시지는 주요 이벤트와 병합할 수 있다.

- 진행 상태
- 추가 증거
- 로그 참조
- 비차단 질문

### 10.9 사람용 메시지 관측

SQLite에 메시지가 commit된 후 사람용 관측 Hook을 별도로 실행할 수 있다.

```text
SQLite commit
→ UDP wake-up
→ message_echo_hook.sh
→ echo로 콘솔 또는 로컬 로그 출력
```

이 출력은 SQLite 메시지 큐, Thread Snapshot, Context Compiler 또는 Agent 프롬프트로 다시 들어가지 않는다. 따라서 사람이 역할 간 메시지와 Git OID를 확인할 수 있지만 Agent token은 소비하지 않는다.

보다 자세한 확인이 필요하면 Python Message Viewer가 SQLite payload의 `repo_id`, `base_oid`, `head_oid`를 읽고 로컬 Git에서 다음을 복원한다.

- 변경 파일
- diff 통계
- commit 목록
- 선택적 실제 diff

## 11. Context Compiler

### 11.1 역할

Context Compiler는 Agent에게 전체 저장소와 전체 메시지를 전달하지 않고, 현재 역할이 다음 결정을 내리는 데 필요한 최소한의 증거 문맥을 구성한다.

```text
SQLite
→ 목표, 현재 상태, 질문, 결정, finding

Git
→ base/head diff, 변경 파일, commit 이력

Semantic Index
→ 심볼, 호출자, 구현체, 영향 테스트

Artifact Store
→ 빌드·테스트·분석 결과

Role Lens
→ 역할별 우선순위
```

### 11.2 Repository Registry

Git OID를 찾으려면 메시지의 `repo_id`를 실제 로컬 저장소로 해석해야 한다.

```text
repo_id: product
bare_repo: C:\agent-team\repositories\product.git
default_branch: integration
index_path: C:\agent-team\indexes\product
```

### 11.3 Git 변경 탐색

Context Compiler는 메시지의 `changed_paths`를 신뢰하지 않고 Git에서 다시 계산한다.

```text
1. base_oid와 head_oid commit 존재 검증
2. diff name-status와 rename 탐지
3. diff stat 계산
4. 변경 hunk와 포함 심볼 추출
5. base..head commit 목록 추출
6. 역할에 필요한 경로와 심볼 확장
```

### 11.4 Semantic Code Index

Git은 변경된 파일과 줄은 알지만 호출자·상속·구현체·영향 테스트는 알지 못한다.

언어별 분석기를 통해 Semantic Index를 구성한다.

| 언어 | 분석기 예 |
|---|---|
| C/C++ | clangd, libclang, `compile_commands.json` |
| C# | Roslyn |
| Rust | rust-analyzer |
| Python | AST, Pyright |
| Java/Kotlin | JDT, Kotlin Analysis API |
| 범용 보조 | Tree-sitter, `rg` |

Semantic 분석은 `head_oid`에 고정된 detached analysis worktree에서 수행한다.

### 11.5 관련 코드 확장 단계

```text
Level 1: 변경 hunk와 포함 함수·클래스
Level 2: 직접 호출자·피호출자
Level 3: 공개 인터페이스·구현체·관련 테스트
Level 4: 검토자가 추가 요청한 증거
```

### 11.6 역할별 Context Lens

| 역할 | 우선 Context |
|---|---|
| PM | 사용자 목표, 완료 조건, 범위, 미결 요구사항 |
| PL | Plan, 의존성, 소유권, Gate, 통합 상태 |
| TA | 인터페이스, ADR, 상태 전이, 참조 관계, 기술 위험 |
| Developer | Work Item, 승인 설계, 관련 코드, 실패 테스트 |
| QA/SDET | 완료 조건, 동작 변경, 위험 경로, 테스트 환경 |
| Build/Release | 빌드 구성, 의존성, 설치·복구·롤백 |

### 11.7 Context Snapshot

원본 메시지는 삭제하지 않고 Snapshot으로 압축한다.

```json
{
  "snapshot_id": "snapshot-W42-07",
  "thread_id": "thread-W42",
  "target_role": "ta",
  "covered_through_message_id": "msg-106",
  "objective": "StateManager 종료 경합 수정",
  "current_state": "UNDER_REVIEW",
  "current_revision": 3,
  "base_oid": "71ae234f9c...",
  "head_oid": "d920f31a82...",
  "approved_decisions": ["ADR-17"],
  "open_findings": ["F-01", "F-03"],
  "required_action": "ARCHITECTURE_REVIEW",
  "evidence_refs": []
}
```

다음 Agent 호출은 다음으로 구성한다.

```text
최신 Snapshot
+ Snapshot 이후 신규 메시지
+ 최신 Git delta
+ 역할별 추가 증거
```

### 11.8 압축하면 안 되는 정보

- 승인된 요구사항과 완료 조건
- 미해결 질문
- 미해결 finding
- ADR 및 계약 변경
- 승인·테스트 대상 commit OID
- 담당 역할
- 다음 필수 행동
- 예산과 반복 제한

### 11.9 Review Packet

검토자는 구현자의 자연어 요약만 받지 않는다.

```text
결정 요청
요구사항과 완료 조건
실제 base/head diff
변경 파일·심볼
영향받는 계약
이전 finding과 해결 매핑
빌드·테스트 증거
남은 위험
생략된 Context
원본 Evidence 참조
```

Context가 부족하면 검토자는 `REJECTED`가 아니라 `NEED_MORE_CONTEXT`를 반환한다.

## 12. Local Git 및 Workspace 격리

### 12.1 저장소 구조

```text
agent-team/
├─ repositories/
│  └─ product.git
├─ worktrees/
│  ├─ W-42-dev-1/
│  ├─ W-43-dev-2/
│  ├─ W-42-ta-review/
│  ├─ W-42-qa/
│  └─ W-42-integration/
├─ build/
│  ├─ W-42-dev-1/
│  ├─ W-43-dev-2/
│  └─ W-42-integration/
├─ artifacts/
└─ state/
   └─ agent-team.db
```

중앙 저장소는 로컬 bare Git이다. 원격 서버를 사용하지 않는다.

### 12.2 격리 단위

```text
work item
+ revision
+ branch
+ worktree
+ build output
```

여러 Agent는 같은 프로젝트에 동시에 commit할 수 있지만 같은 workspace 또는 같은 branch에는 동시에 commit할 수 없다.

### 12.3 Branch 명명

```text
work/<work_item>/<role>/<revision>
integration/<work_item>
release/<goal>/<revision>
```

장기간 유지되는 `agent/dev_1` 형태의 branch는 사용하지 않는다.

### 12.4 단일 작성자 규칙

- Work branch: 해당 Work Item 수행 Agent만 작성
- Review worktree: 읽기 전용 또는 Review Artifact만 생성
- QA worktree: 테스트 실행 전용
- Integration branch: PL 또는 Integration Controller만 작성
- Main/Release branch: Build/Release Gate 통과 후 승격

### 12.5 Write Scope

PL은 Work Item에 읽기·쓰기 범위를 지정한다.

```json
{
  "work_item_id": "W-42",
  "read_scope": [
    "src/platform/**"
  ],
  "write_scope": [
    "src/runtime/**",
    "tests/runtime/**"
  ]
}
```

병렬 Work Item의 write scope가 겹치면 다음 중 하나를 선택한다.

1. 의존성을 추가하여 순차 실행
2. 하나의 Work Item으로 병합
3. 공통 계약 변경을 선행 작업으로 분리
4. PL이 충돌 가능성과 통합 책임을 명시적으로 승인

### 12.6 Review 및 QA 고정

검토자와 QA는 branch 이름이 아니라 정확한 `head_oid`의 detached worktree를 사용한다.

검토 후 branch head가 바뀌면 기존 승인을 무효화한다.

### 12.7 Workspace 생명주기

```text
ALLOCATED
→ base_oid에서 branch/worktree 생성
→ Agent 작업
→ commit 생성
→ Review Request
→ Review/QA
→ Integration
→ Archive Ref 생성
→ Workspace 정리
```

## 13. 역할 간 검토·승인 프로토콜

### 13.1 기본 상태 전이

```text
ASSIGNED
→ IN_PROGRESS
→ SUBMITTED
→ UNDER_REVIEW
├─ APPROVED → 다음 Gate
├─ CHANGES_REQUESTED → REWORK → RESUBMITTED
├─ NEED_MORE_CONTEXT → Context 확장 → 동일 revision 재검토
└─ REJECTED → 상위 역할 판단 또는 종료
```

### 13.2 검토 책임

| 검토 대상 | 수행자 | 책임 검토자 | Gate |
|---|---|---|---|
| 요구사항·완료 조건 | PM | PL | Requirement Gate |
| Plan·의존성·소유권 | PL | TA 및 관련 Developer | Plan Gate |
| 아키텍처·계약 | TA | PL 및 영향 모듈 | Architecture Gate |
| 구현 | Developer | TA 또는 독립 Developer | Code Review Gate |
| 테스트 | QA/SDET | 검증 정책 | Quality Gate |
| 설치·배포 | Build/Release | QA 및 PL | Release Gate |

### 13.3 재검토 Context

재검토에서는 전체 변경을 반복 전달하지 않는다.

```text
이전 검토 대상 OID
→ 새 OID delta
+ 이전 finding
+ finding별 수정 증거
+ 새 테스트 결과
+ 새로 발생한 위험
```

### 13.4 자동 Gate

최종 통합은 구조화된 검토 결과와 deterministic policy를 함께 사용한다.

```text
Architecture Review == APPROVED
AND Code Review == APPROVED
AND Regression Test == PASSED
AND Build == PASSED
AND Critical Finding == 0
AND Acceptance Criteria == SATISFIED
AND approved_oid == tested_oid == merge_target_oid
```

## 14. Loop Engineering

Loop Engineering은 단순히 같은 프롬프트를 반복하는 것이 아니다.

각 iteration에서 다음을 수행하는 제어 구조다.

```text
관찰
→ 목표와 실제 상태의 차이 분석
→ 실패 원인 분류
→ Context 갱신
→ 다음 역할과 Skill 선택
→ 작업 수행
→ 증거 생성
→ 계속·재계획·종료 판정
```

### 14.1 Module Development Loop

```text
DISCOVERING
→ DESIGNING
→ READY_FOR_IMPLEMENTATION
→ IMPLEMENTING
→ VERIFYING
→ REVIEWING
├─ APPROVED → INTEGRATING → COMPLETED
├─ CODE_REWORK → IMPLEMENTING
├─ DESIGN_REWORK → DESIGNING
├─ NEED_EVIDENCE → DISCOVERING
└─ CROSS_MODULE_IMPACT → PARENT_REPLAN
```

한 iteration의 결과:

```json
{
  "module": "parser-core",
  "loop_id": "loop-parser-core-01",
  "iteration": 4,
  "status": "COMPLETED",
  "head_oid": "d920f31a82...",
  "produced_artifacts": [],
  "verification_evidence": [],
  "contract_changes": [],
  "unresolved_risks": [],
  "affected_modules": []
}
```

### 14.2 Goal Supervisory Loop

Goal Loop는 하위 Module Loop를 직접 대신 수행하지 않는다. 다음을 책임진다.

- Goal 상태와 완료 증거 관리
- Workstream 생성과 종료
- Module 의존성 및 우선순위
- 계약 변경에 따른 영향 모듈 재계획
- 통합 실패 라우팅
- 예산·반복 제한 관리
- Goal 완료 또는 `BLOCKED` 판정

```text
DEFINE_GOAL
→ DECOMPOSE
→ RUN_WORKSTREAMS
→ INTEGRATE
→ SYSTEM_VERIFY
├─ PASSED → GOAL_COMPLETED
├─ MODULE_DEFECT → 해당 Module Loop
├─ CONTRACT_CONFLICT → TA 판단 후 재계획
├─ PLAN_INVALID → Sequential Thinking 재계획
├─ EVIDENCE_MISSING → 검증 Loop
└─ POLICY_BLOCKED → BLOCKED
```

### 14.3 상·하위 Loop 계약

상위 Goal Loop가 Module Loop에 전달:

- 모듈 목표와 범위
- Goal에서 투영된 완료 조건
- 입력·출력 계약
- 의존 모듈
- 허용된 변경 범위
- 예산과 iteration 제한
- 필수 Gate

Module Loop가 Goal Loop에 반환:

- 완료·실패·차단 상태
- commit 및 Artifact
- 테스트·검토 증거
- 계약 변경
- 영향받는 모듈
- 미해결 위험
- 재계획 필요 여부

### 14.4 Airflow와 Loop 매핑

장기 실행 Python `while`로 Loop를 유지하지 않는다.

```text
Loop 상태         → SQLite에 영속
한 iteration      → 하나의 DagRun
다음 iteration    → 상태 변경 이벤트로 새 DagRun
Goal Supervisor   → 하위 Loop 결과 이벤트로 실행
```

### 14.5 반복 실패 정책

동일 실패가 반복되면 프롬프트만 다시 실행하지 않는다. 다음 중 하나 이상을 변경해야 한다.

- Context
- 가설
- 설계
- 담당 역할
- Skill
- 작업 범위
- 검증 방법

최대 반복 횟수 또는 예산을 초과하면 `BLOCKED`로 전환한다.

## 15. 실패 및 복구

| 실패 유형 | 처리 |
|---|---|
| 모델 API·MCP 일시 실패 | Airflow Retry |
| Agent 프로세스 중단 | Message lease 만료 후 재전달 |
| SQLite 알림 누락 | Periodic reconciliation |
| Context 부족 | `NEED_MORE_CONTEXT` |
| 코드 검토 실패 | 새 Rework revision |
| 테스트 실패 | 구현 담당자에게 finding 전달 |
| Plan 의존성 오류 | Sequential Thinking 재계획 |
| Git OID 누락 | `CONTEXT_SOURCE_MISSING` |
| 승인 후 HEAD 변경 | 기존 Gate 무효화 및 재검 |
| 통합 충돌 | Integration Work Item 생성 |
| 반복 실패·예산 소진 | Goal 또는 Module `BLOCKED` |

## 16. 로컬 전용 저장 정책

- 모든 Git repository는 로컬 경로에 둔다.
- SQLite DB는 로컬 파일만 사용한다.
- Artifact는 로컬 content-addressed 경로 또는 Work Item 경로에 저장한다.
- 원격 Git, S3, SaaS Message Queue를 사용하지 않는다.
- 외부 모델 API 사용 여부는 별도의 실행 정책으로 관리하며 본 문서의 저장 구조와 분리한다.
- 비밀정보와 대형 binary는 SQLite 메시지에 저장하지 않는다.

## 17. 관측성과 감사

모든 주요 판단은 다음 식별자를 가져야 한다.

```text
goal_id
plan_id
workstream_id
work_item_id
loop_id
iteration
thread_id
message_id
repo_id
base_oid
head_oid
artifact_ref
decision_id
finding_id
dag_run_id
```

다음 질문에 항상 답할 수 있어야 한다.

- 누가 어떤 역할로 작업했는가?
- 어떤 Context Snapshot을 받았는가?
- 어떤 commit을 검토했는가?
- 어떤 테스트가 어느 commit에서 실행됐는가?
- 승인 실패 이유는 무엇이었는가?
- 어떤 revision에서 해결됐는가?
- 최종 통합 commit은 어떤 증거로 승인됐는가?

## 18. Python 스크립트 구성

모든 실행 스크립트는 프로젝트 `/scripts` 디렉터리에 둔다.

| 파일 | 책임 |
|---|---|
| `agent_team_queue.py` | SQLite schema, enqueue, claim, lease, ACK, retry, snapshot, outbox |
| `agent_team_dispatcher.py` | UDP wake-up, polling fallback, outbox 전달 |
| `agent_team_context.py` | Repository Registry, Git OID 검증, diff·commit Context 구성 |
| `agent_team_message_viewer.py` | 사람이 SQLite 메시지와 Git 변경을 조회 |
| `agent_team_taskflow.py` | Airflow TaskFlow Module iteration DAG |
| `message_echo_hook.sh` | commit된 메시지를 사람에게 `echo`로 출력 |
| `test_agent_team_scripts.py` | Queue·Snapshot·Git Context 단위 테스트 |
| `requirements.txt` | 실제 DagRun용 Apache Airflow 런타임 버전 |

Python 메시지 큐와 TaskFlow DAG는 동일한 Message Envelope와 Git OID 계약을 사용한다.

`apache-airflow-task-sdk`는 DAG 작성과 Task SDK 사용을 위한 최소 패키지다. Scheduler와 API Server를 포함한 실제 실행 환경에서는 전체 `apache-airflow`를 설치하며, 전체 패키지가 Task SDK를 의존성으로 설치한다.

## 19. 단계적 구현 순서

### Phase 1. 팀·업무 상태 기반

- Agent 역할 Registry
- Skill Registry
- Goal·Workstream·Work Item schema
- SQLite 메시지 큐
- Transactional Outbox
- Local Dispatcher

### Phase 2. 계획 계층

- Sequential Thinking MCP Adapter
- Serena semantic source evidence and bounded project-knowledge adapter
- Plan IR schema
- Plan Validator
- Dependency 및 write scope 충돌 검사

### Phase 3. Git·Workspace

- Local bare repository registry
- Branch·worktree allocator
- Workspace lease
- Build output 분리
- Commit OID 기반 Artifact 계약

### Phase 4. Context Compiler

- Git diff collector
- Semantic Index adapter
- Thread State projection
- Role Context Snapshot
- Review Packet

### Phase 5. TaskFlow 실행

- Goal Supervisor DAG
- Module Iteration DAG
- Review Gate DAG
- Integration DAG
- Retry와 Rework 분리

### Phase 6. Loop Engineering

- Module 상태 머신
- Goal 상태 머신
- iteration budget
- 반복 실패 분류
- 자동 재계획
- 완료 및 `BLOCKED` 정책

### Phase 7. 자율 승인 강화

- deterministic Gate Engine
- 다중 검토 결과 종합
- OID 일치 검증
- 통합 회귀 테스트
- Release·rollback 검증

## 20. 초기 완료 조건

다음 시나리오가 사람의 중간 개입 없이 실행되면 1차 아키텍처가 성립한 것으로 본다.

1. Goal 등록
2. Sequential Thinking이 Plan IR 생성
3. Plan 검증 및 TaskFlow 실행
4. 두 개 이상의 Work Item을 독립 worktree에서 병렬 구현
5. SQLite 메시지로 검토 요청 전달
6. Context Compiler가 Git OID 기반 Review Packet 생성
7. 검토자가 `CHANGES_REQUESTED` 반환
8. 구현 Agent가 새 revision 제출
9. 동일 finding에 대한 재검 통과
10. QA가 같은 OID를 테스트
11. Integration Controller가 승인된 commit 통합
12. Goal Loop가 증거를 확인하고 Goal 완료

## 21. 미결 설계 결정

다음 항목은 구현 전에 별도 ADR로 확정한다.

- Local Dispatcher의 Windows IPC 방식
- SQLite schema migration 도구
- Airflow 배치와 로컬 실행 방식
- Plan IR의 JSON Schema 또는 Pydantic 모델
- 언어별 Semantic Index 우선순위
- Context token budget 산정 방식
- Snapshot 압축 Trigger
- Artifact 보존 및 정리 기간
- rejected branch와 commit의 archive 정책
- 자동 통합 허용 범위
- 보안·배포 변경의 추가 승인 정책

## 22. 최종 구조 요약

```text
Agent Team
역할·책임·권한
        ↓
Sequential Thinking MCP
Goal을 Plan IR로 분해
        ↓
Airflow TaskFlow API
Plan과 Agent Task 실행
        ↓
SQLite Queue + Hook
역할 간 업무 메시지와 상태
        ↓
Context Compiler
Git·메시지·증거를 역할별로 압축
        ↓
Local Git Worktree
독립 구현·검토·테스트
        ↓
Module Development Loop
모듈 단위 반복 개발
        ↓
Goal Supervisory Loop
전체 Workstream 조정과 최종 완료
```

이 구조에서 Airflow는 실행 엔진이고, Sequential Thinking은 계획 엔진이며, SQLite는 조직의 업무 통신 계층이고, Git은 코드와 변경 증거의 기준이다. Context Compiler가 각 역할에 적절한 문맥을 배치하고, Module Loop와 Goal Loop가 이를 지속적으로 제어함으로써 로컬 자율 개발팀이 완성된다.
