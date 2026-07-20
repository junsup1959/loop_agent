# 저장소 구성 안내

## 목적

이 문서는 현재 작업 트리의 상태, 검증을 거친 Codex 네이티브 전달 번들, 대량 리서치 제어면, 자동 컨텍스트 적용 범위, 그리고 선택형 MCP 도구 정책을 설명한다.

## 현재 작업 트리

현재 작업 트리는 이전 구조를 유지하고 있다. 이 세션에서는 실제 `.agents` 및 `.codex` 디렉터리에 쓸 수 없으므로, 검증된 전달 번들로 라이브 작업 트리를 직접 교체하지 않았다.

저장소 루트에는 `AGENTS.md`가 없다. 따라서 이 저장소에서 수행하는 다른 설정 작업에 에이전트 팀 지침이 자동 상속되지 않는다. 전달 번들에도 `AGENTS.md`를 넣지 않았으므로, 번들을 다른 프로젝트에 덮어쓸 때 기존 프로젝트의 `AGENTS.md`를 덮어쓰거나 자동 컨텍스트를 추가하지 않는다.

| 경로 | 현재 상태 | 비고 |
| --- | --- | --- |
| `skills/` | 소스 스킬 패키지 21개 | 기존 프로젝트 소스 구조 |
| `agents/` | 팀 템플릿, 프로필, 좌석 레지스트리, Serena 정책 | 기존 스크립트가 사용하는 소스 구조 |
| `.codex/skills/` | 부분 스킬 복사본 19개 | Codex 네이티브 저장소 스킬 위치가 아닌 이전 미러 |
| `.codex/agents/` | 좌석 TOML 8개 | Codex 프로젝트 커스텀 에이전트 위치 |
| `.codex/config.toml` | 기존 라이브 설정 | 전달 번들의 검증 대상과 별개 |
| `.agent-team/` | 로컬 런타임 상태 | SQLite, 아티팩트, worktree, npm 의존성, 서비스 상태 |

## 검증된 전달 번들

깨끗한 전달 세트는 다음 경로에 있다.

```text
output/agent-team-codex-native/
```

구조는 다음과 같다.

```text
.agents/skills/                 Codex 네이티브 저장소 스킬 24개
.codex/agents/                  Codex 프로젝트 커스텀 좌석 에이전트 8개
config/agent-team/              로컬 스크립트용 팀 설정과 명시적 컨텍스트 문서
scripts/                         런타임, 검증, RTK PreToolUse 훅 스크립트
bundle-manifest.toml             전달 목록과 검증 기록
```

번들에는 `AGENTS.md`, `skills/`, `agents/`, `.codex/skills/`, Python 바이트코드 캐시를 의도적으로 포함하지 않는다. 이 번들은 현재 라이브 작업 트리를 자동으로 활성화하거나 교체하는 파일이 아니라, 프로젝트 루트에 적용할 수 있는 overlay 전달 세트다.

## 네이티브 스킬 이름

Codex 스킬 식별자는 hyphen-case를 사용한다. 전달 매니페스트에는 사용자가 요청한 논리 식별자도 함께 기록한다.

| 논리 식별자 | 설치되는 Codex 스킬 |
| --- | --- |
| `goal_loop` | `$goal-loop` |
| `moudle_loop` | `$module-loop` |
| `research_loop` | `$research-loop` |

`$goal-loop`는 목표 계획, 모듈 조율, 통합, 증거 기반 완료 판정을 관리한다. `$module-loop`는 하나의 제한된 모듈을 조사, 구현, 검증, 리뷰, 재작업, 통합 단계로 진행한다.

`$research-loop`는 웹·로컬 파일·저장소 소스가 일반 조사 컨텍스트보다 클 때, 원문 보관, 샤드 분할, 주장·근거 병합, 독립 검증, 충돌 해소, 최종 결론을 제어한다. 영구 리서처 좌석을 추가하지 않고 기존 좌석을 임시 research lane으로 배정한다.

## 컨텍스트 주입 정책

각 좌석 바인딩 활성화는 역할별 최소 컨텍스트 패킷을 새로 컴파일해야 한다. 패킷에는 다음만 들어갈 수 있다.

- 대상 thread, work item, 역할, 좌석;
- 명시적으로 선택된 스킬;
- 관련 SQLite 스냅샷과 메시지;
- 검증된 Git base/head OID;
- 선택된 변경 경로와 단계별 증거;
- `config/agent-team/activation-instructions.md`;
- `config/agent-team/recommended-tools.md`.

두 Markdown 문서는 자동 발견되지 않는다. Context Compiler가 경로, SHA-256, 문자 수를 아티팩트에 고정하고 runner 직전에 다시 검증하여 주입한다. 따라서 번들 내부 지침은 필요한 팀 활성화에만 들어가며, 다른 Codex 작업의 자동 컨텍스트가 되지 않는다.

관련 없는 thread, work item, 역할, 선택하지 않은 스킬, 전체 소스 덤프, 전체 Serena 메모리, 참조되지 않은 과거 대화는 제외한다. 패킷의 예산, 제외 항목, 선택 사유도 컨텍스트 아티팩트에 남긴다.

## 대량 리서치와 아티팩트

전달 번들의 `scripts/agent_team_research.py`는 로컬 SQLite 원장과 로컬 아티팩트 저장소를 사용한다. 원문, 정규화 텍스트, 샤드, 전체 요약, 주장, 충돌, 결론은 아티팩트 저장소에 남고 SQLite 메시지·TaskFlow·컨텍스트에는 ID, 해시, 경로, 근거 참조만 남는다.

요약의 10% 비율은 컨텍스트 절감용 **권장치**다. 비율이나 권장 크기를 초과해도 요약을 거절·절단·폐기하지 않고, 실제 비율과 경고를 메타데이터에 기록한다. 컨텍스트 패킷에 들어가지 않는 전체 아티팩트는 로컬에 보존하며, 다음 활성화에서 더 좁은 샤드·근거 범위를 명시적으로 선택한다.

`agent_team_research_iteration` TaskFlow DAG는 연구 ID와 원장이 허가한 참조만 사용한다. DagRun이 임의 `artifact_paths`를 전달할 수 없으며, 일반 작업의 증거 아티팩트도 해당 work item의 `evidence/<work_item_id>/` 경로로 제한된다. 이 규칙으로 다른 lane의 결과·컨텍스트를 임의로 주입하지 못하게 한다.

TaskFlow는 제어면에서 컨텍스트 아티팩트 경로와 참조만 전달한다. 로컬 runner 시작 직전에만 Context Compiler가 해시를 다시 검증한 선택 증거를 제한된 예산 안에서 materialize한다. 리서치 runner의 발신 메시지는 활성 research ID와 근거 참조를 필수로 하며, `text`, `summary`, `raw`, `excerpt` 같은 원문성 필드를 포함하면 큐에 넣기 전에 거절된다.

## 권장 도구와 MCP

Serena와 Sequential Thinking은 매 활성화에 명시적으로 주입되는 **권장 도구 컨텍스트**다. 권장 도구 문서는 MCP가 설치되지 않았거나 일시적으로 사용할 수 없어도 패킷에서 사라지지 않는다. 다만 최초 설치·재설치의 성공 조건은 두 MCP의 준비와 검증이므로, 사람은 Codex 바깥의 BAT 두 개를 순서대로 실행해야 한다.

- Serena가 가능하면 심볼 탐색, 참조 추적, 제한된 소스 발췌, 온보딩, 느리게 변하는 프로젝트 메모리에 사용한다. 사용할 수 없으면 Git, 로컬 검색, 검증된 파일 읽기로 대체한다.
- Sequential Thinking이 가능하면 작업 분해, 의존성 추론, 대안 비교, Plan IR/Task DAG 수정에 사용한다. 사용할 수 없으면 같은 계획 근거를 Task Artifact에 직접 기록한다.
- 도구의 사용 가능 여부는 승인 권한, 쓰기 범위, 외부 접근 권한, 컨텍스트 확장 권한을 부여하지 않는다.

의존성 설치와 프로젝트 설정은 의도적으로 분리한다. 활성 상태와 마지막으로 확인된 Serena URL은 `.agent-team/state/mcp-capabilities.json`에 저장한다.

```powershell
# 1. Install Serena CLI and project-local Sequential Thinking only.
.\scripts\install_mcp_dependencies.bat

# 2. Configure the project, both MCP entries, and strict checks only.
.\scripts\setup_agent_team.bat

# Repair or inspect already-installed capabilities without provisioning.
python .\scripts\init_agent_team.py --configure-mcp serena
python .\scripts\init_agent_team.py --check-mcp serena
python .\scripts\init_agent_team.py --check-mcp sequentialthinking
```

설정 배치가 생성한 MCP만 `.codex/config.toml`에 들어가며 둘 다 `required = false`를 사용한다. 일반 `init`과 `--check`는 MCP 준비 상태를 점검하거나 MCP 장애 때문에 실패하지 않는다. MCP만 엄격히 확인하려면 `--check-mcp`를 사용한다. 즉 설치 배치의 성공 조건과 이후 런타임의 권장 도구 정책은 분리되어 있다.

## RTK 명령 실행 정책

번들에는 `config/agent-team/RTK.md`와 `scripts/rtk_pre_tool_use.py`가 있다. 초기화기가 생성하는 프로젝트 로컬 Codex `PreToolUse` 훅은 이 정책을 읽고 다음을 수행한다.

- 단순 지원 명령은 `rtk git status` 형식으로 자동 재작성한다.
- 이미 `rtk`로 시작한 명령은 그대로 허용한다.
- 복합 명령이나 안전하게 재작성할 수 없는 명령은 `rtk proxy <executable> <arguments>` 형식으로 다시 제출하도록 차단한다.

이 훅은 신뢰된 프로젝트의 생성된 `.codex/config.toml`을 Codex가 다시 로드한 뒤에만 적용된다. 일반 에이전트 컨텍스트에 `RTK.md` 전체를 반복 주입하지 않으므로 명령마다 불필요한 토큰을 사용하지 않는다.

## `bundle-manifest.toml`의 용도

`bundle-manifest.toml`은 전달 메타데이터다. 의도한 네이티브 경로, 번들 목록, 논리 스킬 별칭, 검증 결과, 선택형 MCP 정책, 활성화 전제조건을 기록한다.

Codex, 현재 Python 런타임, TaskFlow DAG는 이 파일을 자동으로 읽지 않는다. 사람 검토자나 이후 설치기/CI 검증기가 전달 세트의 완전성을 확인하는 데 쓰는 기록이다. `config.toml`처럼 취급하면 안 되며, MCP 서버를 설정하거나 에이전트를 시작하지도 않는다.

## 활성화 전제조건과 제약

번들에는 `.codex/config.toml`이 포함되지 않는다. `scripts/init_agent_team.py`가 프로젝트 경로에 맞는 설정을 생성해야 한다. 라이브 `.codex/config.toml`이 관리되지 않는 기존 파일이면, 초기화기는 안전을 위해 덮어쓰지 않는다.

먼저 `scripts\\install_mcp_dependencies.bat`로 Serena CLI와 프로젝트 로컬 Sequential Thinking을 설치하고, 그 다음 `scripts\\setup_agent_team.bat`로 프로젝트 설정, 인덱스, 메모리, loopback Streamable HTTP 서비스와 두 MCP 구성을 만든다. 두 BAT는 Codex 세션이 아닌 일반 terminal에서만 실행한다.

현재 Windows/Python 환경에서는 구성 검증, SQLite 큐, Git 컨텍스트, runner 어댑터, outbox, viewer, 컨텍스트 격리 검증을 실행할 수 있다. 실제 Airflow DAG 실행은 WSL 또는 Linux가 필요하다.

## 검증 기록

번들은 다음 검증을 통과했다.

- 프로젝트 로컬 스킬 23개 검증(bootstrap Skill 제거);
- 커스텀 좌석 에이전트 8개 검증;
- `$goal-loop`, `$module-loop` 스킬 구조 검증;
- `$research-loop` 스킬 구조 검증;
- 로컬 리서치 원장의 수집→샤딩→요약→주장→충돌→결론 흐름 검증;
- 10% 초과 요약이 경고만 남기고 전체 내용 그대로 저장됨을 검증;
- 리서치 컨텍스트가 원문 대신 참조만 선택하고, TaskFlow가 원장이 허가한 아티팩트만 주입함을 검증;
- PL 활성화에서 선택한 `$goal-loop`과 두 명시적 컨텍스트 문서만 주입됨을 검증;
- 권장 도구 문서가 runner 요청까지 전달되고, 선택하지 않은 스킬·다른 역할·다른 work item·다른 thread·비관련 경로는 제외됨을 검증;
- MCP 의존성 설치와 프로젝트 설정 BAT가 분리되고, 각각의 dry-run이 다른 쓰기 순서를 출력함을 검증;
- dependency install은 Codex MCP 구성을 생성하지 않고, setup configuration은 Sequential Thinking 설치 동작을 호출하지 않음을 검증;
- MCP가 비활성인 상태에서도 코어 초기화와 `--check`가 성공하고 생성된 설정에 MCP 서버 블록이 없음을 검증;
- RTK PreToolUse 훅의 단순 명령 재작성과 복합 명령 차단을 검증;
- 임시 테스트 소스, 임시 runtime, 바이트코드 캐시가 전달 번들에 남지 않음을 검증.
