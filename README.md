# 프로젝트 로컬 Codex Agent Team

이 저장소는 사람 개발팀의 책임 구조를 유지하면서, Codex 에이전트가 로컬 환경에서 작업을 분담·검토·인계할 수 있게 만드는 프로젝트별 운영 번들을 구현한다. 역할은 고정하고 전문성은 작업마다 Skill로 선택하며, 상태는 SQLite, 코드 증거는 로컬 Git, 큰 자료는 로컬 artifact store, 실행 순서는 Airflow TaskFlow로 분리한다.

현재 검증·재배포된 전달물은 [output/agent-team-codex-native](output/agent-team-codex-native)이다. 이 README는 그 번들에 실제로 들어 있는 구현을 기준으로 작성했다.

## 핵심 상태

| 항목 | 현재 상태 |
| --- | --- |
| 전달 형식 | 프로젝트에 덮어 적용하는 로컬 overlay 번들 |
| 네이티브 Skill | 24개 |
| 논리 좌석 | 8개 |
| 역할 템플릿 | 6개 |
| 상태·메시지 | 로컬 SQLite |
| 코드 증거 | 로컬 Git OID, diff, commit 이력 |
| 대량 조사 증거 | 로컬 SQLite research ledger + 로컬 artifact store |
| TaskFlow 진입점 | module iteration, research iteration |
| 원격 Git·원격 큐·원격 artifact storage | 사용하지 않음 |
| Serena·Sequential Thinking | 권장 도구이며 필수 계약이 아님 |

## 먼저 알아둘 점

- 실제 전달 원본은 `output/agent-team-codex-native/`다. 루트의 오래된 `skills/`, `agents/`, `.codex/skills/` 등을 배포 원본으로 사용하지 않는다. 자세한 경계는 [REPOSITORY_CONFIGURATION.md](REPOSITORY_CONFIGURATION.md)에 있다.
- 번들은 `AGENTS.md`를 의도적으로 포함하지 않는다. 다른 프로젝트의 자동 컨텍스트 규칙을 덮어쓰지 않기 위해서다.
- 이 번들은 전역 Codex Home을 수정하지 않는다. 대상 프로젝트 안에서만 `.agents/`, `.codex/`, `config/`, `scripts/`를 사용한다.
- 배포만으로 자동 활성화되지는 않는다. **일반 PowerShell**에서 대상 프로젝트의 초기화 스크립트를 실행해 `.codex/config.toml`을 만든 뒤, 해당 프로젝트를 신뢰하고 Codex를 다시 열어야 한다. Codex 세션 안에서 `.codex/`를 쓰려고 보호 범위를 넓히지 않는다.
- Goal/Module/Research loop는 현재 사용할 수 있는 Skill 워크플로우다. 아직 완전한 무인 Plan IR 컴파일러나 자동 역할 배정 시스템이 구현되었다는 뜻은 아니다.

## 설계 원칙

```text
조직 역할       = 누가 책임·승인·통합을 맡는가
작업 Skill       = 이번 작업을 어떤 전문성으로 수행하는가

SQLite           = 메시지, 상태, 인계, 결정, delivery
로컬 Git         = 코드, commit, diff, OID 기반 증거
로컬 artifact    = 큰 자료, 테스트 결과, 연구 원문·요약·결론
Context Compiler = 현재 역할에 필요한 최소 증거만 해시 검증 후 주입
TaskFlow         = 미리 확정된 작업 순서, 재시도, 실행 경계
```

에이전트는 상주 프로세스가 아니다. 작업 또는 메시지에 의해 깨워지고, 해당 역할·좌석·Skill·컨텍스트 artifact로 실행한 뒤 결과와 참조를 남기고 다시 유휴 상태가 된다.

## 현재 아키텍처

```text
Goal / Module / Research Skill
            |
            v
  bounded runtime configuration
            |
            v
Airflow TaskFlow DAG (when run on Linux or WSL)
            |
            +--> Context Compiler
            |      + SQLite thread snapshot and messages
            |      + Git base/head OID evidence
            |      + explicitly selected Skills
            |      + hash-pinned activation and recommended-tool guides
            |      + explicitly authorized local artifacts
            |
            +--> external local runner
            |
            v
SQLite messages / outbox / human echo / viewer
            |
            v
PL integration and the next bounded iteration
```

TaskFlow는 순서·재시도·task 경계만 담당한다. 작업 분해, 기술 판단, 컨텍스트 선별, 승인 권한은 각각 PL/TA/Context Compiler/역할 계약의 책임으로 남는다.

## 전달 번들 구조

```text
output/agent-team-codex-native/
├─ .agents/skills/                 # 네이티브 project-local Skills 24개
├─ .codex/agents/                  # 생성·검증된 8개 좌석 에이전트 TOML
├─ config/agent-team/              # 역할, 모델, 예산, 권장 도구, RTK 정책
├─ scripts/                         # Python control plane 및 사람용 도구
└─ bundle-manifest.toml             # 번들 범위·검증 결과 메타데이터
```

`bundle-manifest.toml`은 설치나 MCP 서버를 자동 실행하지 않는 선언형 메타데이터다. 전달 경로, logical Skill 별칭, 검증 상태, 활성화 조건을 사람이 또는 향후 검증 도구가 확인하는 데 사용한다.

## 8개 논리 좌석

좌석 ID는 초기화할 때 `role_한국이름` 형식으로 무작위 생성되고 `config/agent-team/seats/registry.toml`에 영속된다. 반복 초기화는 같은 ID를 보존한다. 이름 재생성은 메시지·artifact 참조를 끊을 수 있으므로 명시적 확인이 필요한 파괴적 제어 동작이다.

| 역할 키 | 조직 책임 | 모델·권한 프로필 |
| --- | --- | --- |
| `pm` | 요구사항, 범위, 우선순위, 완료 조건 | `gpt-5.6-terra`, high, read-only |
| `pl` | 작업 분해·배정, 통합, 기술적 최종 조정 | `gpt-5.6-terra`, high, workspace-write |
| `ta` | 구조, 인터페이스, 기술 위험, 아키텍처 검토 | `gpt-5.6-terra`, high, read-only |
| `dev_1` | 배정된 구현 작업 | `gpt-5.6-luna`, high, workspace-write |
| `dev_2` | 배정된 구현 작업 | `gpt-5.6-luna`, high, workspace-write |
| `dev_3` | 배정된 구현 작업 | `gpt-5.6-luna`, high, workspace-write |
| `qa_sdet` | 테스트 설계·자동화·회귀·복구 검증 | `gpt-5.6-terra`, high, workspace-write |
| `build_release` | 빌드, 의존성, 설치·업데이트·롤백 검증 | `gpt-5.6-terra`, medium, workspace-write |

현재 비용 절감 정책에서는 PM·PL·TA·QA/SDET·Build/Release를 `gpt-5.6-terra`로, 개발 3석을 `gpt-5.6-luna`로 고정한다. `research-lane`은 개발 3석만 허용하므로 단순 읽기·추출·구조화 요약은 Luna에서 실행된다. 연구 계획·합성·검증과 리뷰·승인 판단은 해당 Terra 좌석에 남는다. 이 정책은 역할 책임, Skill 권한, sandbox, reasoning effort를 바꾸지 않는다.

개발 좌석은 특정 언어 또는 모듈에 영구 고정되지 않는다. 예를 들어 같은 `dev_1`이 한 작업에서는 C++ 동시성 Skill을, 다음 작업에서는 Python 또는 빌드 Skill을 받을 수 있다. Skill은 전문성만 보태며 승인 권한, 쓰기 범위, 외부 접근, 모델 선택 권한을 늘리지 않는다.

## Skill 카탈로그

Skill은 자동으로 전부 주입되지 않는다. 좌석과 역할 적합성을 검사한 뒤, 현재 작업에 명시적으로 선택된 `SKILL.md`만 Context Compiler가 넣는다. 카탈로그는 [catalog.toml](output/agent-team-codex-native/.agents/skills/catalog.toml)이 기준이다.

현재 네이티브 Skill 24개는 다음과 같다.

```text
clarify-requirements          plan-delivery
coordinate-task-dag           map-codebase
serena-project-setup          agent-team-bootstrap
goal-loop                     module-loop
research-loop                 review-solution-architecture
debug-runtime-failures        review-code-quality
engineer-test-coverage        engineer-build-release
modernize-legacy-systems      engineer-cpp-systems
engineer-dotnet-desktop       engineer-python
engineer-rust-systems         automate-powershell
engineer-local-data           engineer-electron-desktop
design-desktop-ui             engineer-embedded-devices
```

주요 워크플로우 Skill은 다음과 같다.

| Skill | 역할 |
| --- | --- |
| `$agent-team-bootstrap` | 프로젝트별 팀 control plane 초기화와 선택형 MCP 활성화 순서를 안내 |
| `$serena-project-setup` | Serena 프로젝트 생성·인덱싱·health check·memory·공유 HTTP 서비스 준비 |
| `$goal-loop` | 목표 분해, workstream 조정, 통합과 완료·차단 판정 절차 |
| `$module-loop` | 한 모듈의 조사→설계→구현→검증→리뷰→재작업 절차 |
| `$research-loop` | 대량 자료를 수집·분할·요약·교차검증·결론으로 만드는 임시 연구 workstream |

`bundle-manifest.toml`에는 사용자가 요청한 논리 별칭도 기록되어 있다. `goal_loop`은 `$goal-loop`, 의도적으로 유지된 `moudle_loop`은 `$module-loop`, `research_loop`은 `$research-loop`을 뜻한다.

## 최소 컨텍스트 주입

`scripts/agent_team_context.py`와 `config/agent-team/context-profiles.toml`이 역할별 Context Compiler를 구성한다. 기본 원칙은 전체 저장소, 전체 thread, 전체 Skill 카탈로그, 전체 Serena memory를 한 번에 주입하지 않는 것이다.

각 활성화 packet에는 필요한 범위에서만 다음을 넣는다.

- 현재 `thread_id`, `work_item_id`, 역할, 좌석, iteration;
- 명시적으로 선택되고 역할 적합성이 검증된 Skill;
- 역할별 SQLite snapshot과 그 이후의 관련 메시지;
- Git `base_oid`/`head_oid`, 재계산된 변경 경로·diff·commit evidence;
- 해시와 길이가 고정된 `activation-instructions.md` 및 `recommended-tools.md`;
- profile 예산 안에서 권한이 검증된 artifact;
- 누락·제외 사유가 담긴 `omitted_context`.

컨텍스트 profile은 메시지 수·문자 수, Git 경로·diff·commit, Skill 수·문자 수, artifact 수·문자 수, 전체 packet 크기를 상한으로 둔다. 선택된 artifact가 예산에 맞지 않으면 조용히 자르지 않고 실패 또는 `NEED_MORE_CONTEXT` 경로로 돌려야 한다.

일반 work item은 artifact root 아래의 `evidence/<work_item_id>/` 범위만 읽을 수 있다. 연구 work item은 DagRun에서 임의 `artifact_paths`를 받을 수 없고, research ledger가 선택한 경로만 Context Compiler에 허용한다.

## SQLite 메시지와 사람용 관측

`scripts/agent_team_queue.py`는 durable SQLite queue, lease, ACK/retry, thread snapshot, transactional outbox를 제공한다. `scripts/agent_team_dispatcher.py`는 UDP wake-up과 polling fallback을 제공한다.

에이전트 간 메시지에는 코드 본문이나 큰 자료를 넣지 않는다. Git OID, artifact reference, claim ID, 질문, 결정 delta만 저장한다. 코드와 큰 증거는 Git 또는 로컬 artifact store에 남긴다.

사람은 다음 도구로 전체 대화를 token 소비 없이 볼 수 있다.

```powershell
Set-Location .\output\agent-team-codex-native
python .\scripts\agent_team_message_viewer.py
```

`--limit`을 생략하면 일치하는 전체 메시지를 출력한다. 필요하면 `--thread`, `--role`, `--status`, `--show-diff`, `--watch-seconds`를 추가할 수 있다. viewer와 `scripts/message_echo_hook.sh`는 사람 관측 전용이며, 출력 내용을 agent context나 SQLite 메시지로 되돌려 넣지 않는다.

## Git과 증거 경계

- 모든 코드 변경 증거는 로컬 Git의 `repo_id`, `base_oid`, `head_oid`, diff, commit으로 확인한다.
- Context Compiler는 메시지에 들어온 변경 경로를 신뢰하지 않고 Git OID에서 변경 경로를 다시 계산한다.
- 리뷰·QA·통합 판단은 같은 OID를 가리켜야 한다. 원칙은 `approved_oid == tested_oid == integration_target_oid`다.
- 원격 Git 서버, 원격 coordination service, 원격 artifact storage는 이 번들의 데이터 plane에 포함되지 않는다.

작업별 worktree/branch 격리는 아키텍처·워크플로우 문서에 정의되어 있지만, 이를 자동 할당하는 runtime allocator는 아직 제공되지 않는다. 실제 대상 프로젝트의 Git 운영 규칙과 PL의 work-item 계약으로 연결해야 한다.

## 대량 자료 조사: `$research-loop`

대량의 웹·파일·저장소 자료가 일반 탐색 context보다 클 때만 `$research-loop`를 사용한다. 별도 상주 “리서처” 좌석을 만들지 않고, 기존 좌석을 임시 research lane으로 묶는다. PL이 research plan·lane·병합·최종 결론 통합을 책임지고, TA는 기술 해석, QA/SDET는 재현성과 근거 범위를 독립 검증한다.

```text
research brief
  -> local source ledger
  -> local raw-source artifacts
  -> immutable shards
  -> lane summaries
  -> claim/evidence matrix
  -> conflict review
  -> conclusion artifact
```

`scripts/agent_team_research.py`는 다음 명령을 제공한다.

```text
init, create-run, add-file, add-url, shard-source, record-summary,
add-claim, open-conflict, resolve-conflict, finalize, select-context
```

원문, shard, 전체 요약, claim, conflict, conclusion은 로컬 artifact store에 보존한다. SQLite 메시지와 TaskFlow/XCom 경계에는 ID·해시·상대 경로·artifact reference만 통과한다. runner 직전에는 Context Compiler가 선택된 artifact만 해시 검증하고 profile 예산 안에서 local materialization한다.

### 10% 요약 정책

`source-to-summary = 10%`는 **권장 압축 목표**일 뿐이다. 유효성 게이트, 절단 규칙, 거절 규칙이 아니다.

- 10% 또는 advisory absolute-size를 넘는 요약도 원문 그대로 보존한다.
- 실제 비율과 초과 경고만 research ledger에 기록한다.
- 나중의 역할 context가 너무 크면 전체 요약을 버리지 않고 더 작은 shard, 범위, evidence locator, excerpt를 명시적으로 요청한다.
- 원문 수집 정책이 자료를 차단하는 경우에는 부분 자료를 완전한 자료처럼 조용히 저장하지 않는다.

연구 메시지는 활성 `research_id`와 claim/source/shard/summary/conflict/artifact 참조를 반드시 포함해야 한다. `raw`, `text`, `summary`, `excerpt` 같은 material-content 필드는 enqueue 전에 거절한다.

간단한 로컬 research ledger 시작 예시는 다음과 같다.

```powershell
Set-Location .\output\agent-team-codex-native
python .\scripts\agent_team_research.py `
  --db .\.agent-team\state\research.db `
  --artifact-root .\.agent-team\artifacts `
  init
```

## TaskFlow와 외부 runner

`scripts/agent_team_taskflow.py`에는 두 TaskFlow DAG 진입점이 있다.

| DAG ID | 용도 |
| --- | --- |
| `agent_team_module_iteration` | 일반 module iteration의 runtime config 검증→context compile→runner 실행→결과·메시지 저장 |
| `agent_team_research_iteration` | research ID 기반 ledger 선택→research context compile→runner 실행→reference-only 연구 메시지 저장 |

외부 runner는 번들이 제공하지 않는다. 실행 환경에서 `AGENT_TEAM_RUNNER_COMMAND_JSON`에 local runner 명령 배열을 공급해야 한다. runner는 compiled context artifact의 SHA-256을 다시 확인하고, 선택된 Skill·권장 도구·activation instruction·artifact만 읽어 하나의 호환 JSON 결과를 반환해야 한다.

`scripts/requirements.txt`는 완전한 Airflow runtime인 `apache-airflow==3.3.0`을 고정한다. Task SDK만 설치해서는 scheduler와 API server를 갖춘 실제 Airflow 실행 환경이 만들어지지 않는다.

Airflow는 native Windows 실행을 지원하지 않으므로, 이 번들에서 실제 DAG scheduler/DagRun은 WSL 또는 Linux container에서 실행해야 한다. Windows에서는 초기화, SQLite queue, human viewer, Git context, runner adapter, 설정·정적 검증을 수행할 수 있다.

## 선택형 MCP

Serena와 Sequential Thinking은 activation packet 안에 계속 남는 **권장 도구**다. 설치되지 않았거나 일시적으로 사용할 수 없어도 core initialization이나 현재 작업을 자동으로 `BLOCKED`로 만들지 않는다.

| 도구 | 권장 용도 | 제약 |
| --- | --- | --- |
| Serena | 심볼·참조·구조·영향 분석, 안정적인 프로젝트 지식 재사용 | 웹 crawler나 현재 task state store가 아님 |
| Sequential Thinking | 작업 분해, 의존성 추론, 대안 비교, Plan/Task DAG revision | 역할 권한이나 승인을 대체하지 않음 |

Serena를 활성화하면 `$serena-project-setup`이 프로젝트 생성·인덱싱·health check·memory 초기화와 공유 loopback Streamable HTTP 서비스를 준비한다. 서비스는 `127.0.0.1`의 무작위 영속 포트와 `/mcp` endpoint를 쓰며, 좌석마다 stdio 서버를 새로 띄우지 않는다.

Serena memory에는 프로젝트 개요, 빌드·테스트 명령, 모듈 지도처럼 천천히 변하는 지식만 둔다. 현재 작업 상태, 승인, 최신 테스트, Git OID, branch, 에이전트 대화는 SQLite·Git·artifact store가 책임진다. 공용 Serena memory의 publish/refresh/rename/delete는 PL만 수행하며, 다른 역할은 SQLite로 근거 있는 제안을 보낸다.

생성되는 MCP block은 사용자가 명시적으로 활성화한 경우에만 `.codex/config.toml`에 들어가며 항상 `required = false`다.

## 설치와 초기화

전달 번들을 대상 프로젝트에 overlay한 뒤, 아래 명령은 **일반 PowerShell**에서 대상 프로젝트 루트로 이동해 실행한다. Codex 세션 안에서 실행하지 않는다.

```powershell
Set-Location <target-project> # e.g. C:\project\manager\sdetector-manager

# 수동 core initialization:
python .\scripts\init_agent_team.py

# 쓰지 않고 현재 상태만 점검
python .\scripts\init_agent_team.py --check
```

초기화는 필요한 Python 의존성 확인, Skill 검증, 좌석 registry 생성 또는 보존, `.codex/agents/` 동기화, 프로젝트 로컬 `.codex/config.toml` 생성, `.agent-team/state` 초기화를 수행한다. 생성되는 설정은 `workspace-write`, `approval_policy = "on-request"`, 추가 writable root 없음, network off를 요청한다. 이후 대상 프로젝트를 신뢰하고 Codex를 reload/restart해야 생성된 config와 좌석을 읽는다. 단, 호스트가 `.codex/`를 보호 경로로 유지하면 이 설정이 그 보호를 우회하지는 않는다.

선택형 MCP는 별도로 활성화한다.

```powershell
python .\scripts\init_agent_team.py --enable-mcp serena
python .\scripts\init_agent_team.py --enable-mcp sequentialthinking

python .\scripts\init_agent_team.py --check-mcp serena
python .\scripts\init_agent_team.py --refresh-mcp-config
python .\scripts\init_agent_team.py --disable-mcp serena
```

좌석 또는 Skill 카탈로그만 다룰 때는 다음 명령을 사용한다.

```powershell
python .\scripts\project_agents.py init
python .\scripts\project_agents.py validate
python .\scripts\project_agents.py sync
python .\scripts\project_agents.py list

python .\scripts\project_skills.py validate
python .\scripts\project_skills.py list
python .\scripts\project_skills.py resolve --role pl --skill research-loop
```

좌석 ID를 의도적으로 바꾸는 경우에만 다음 destructive command를 쓴다.

```powershell
python .\scripts\project_agents.py regenerate --confirm-identity-reset
```

## RTK 명령 정책

`config/agent-team/RTK.md`와 `scripts/rtk_pre_tool_use.py`는 초기화가 생성한 프로젝트 로컬 Codex `PreToolUse` hook에서 사용된다. 안전하게 변환할 수 있는 간단한 명령은 `rtk` 접두어로 바꾸고, 복합 명령은 `rtk proxy <executable> <arguments>`를 요구한다.

이 정책은 Codex가 새 `.codex/config.toml`을 신뢰·재로딩한 뒤에만 적용된다. 일반 외부 terminal이나 hook을 우회한 프로세스를 전역적으로 바꾸지 않는다. 사람의 수동 설치 명령과 Codex agent의 도구 호출을 혼동하지 않도록 한다.

## 검증 기록

마지막 번들 재배포 전후에 다음을 확인했다.

- `project_skills.py validate`: project-local Skills 24개 통과;
- `project_agents.py validate`: 역할 템플릿 6개와 좌석 8개 통과;
- 역할 고정 모델 정책: Terra 5석(PM·PL·TA·QA/SDET·Build/Release), Luna 3석(개발) 확인;
- `research-lane`: 개발 3석만 허용되어 단순 읽기·추출·구조화 요약이 Luna로 제한되는 것 확인;
- Serena 준비·project knowledge 검사가 사용자 프로필 설정을 요구하지 않고 대상 프로젝트의 `.serena/project.yml`과 memory layout만 확인하는 것 검증;
- 초기화기가 프로젝트 로컬 `workspace-write`·`on-request` 설정을 생성하고, 추가 writable root와 network access를 열지 않는 것 검증;
- 번들 Python 스크립트 12개 문법 compile 통과;
- TOML 24개 parse 통과;
- `pip check` 통과;
- `message_echo_hook.sh` shell syntax와 stdin echo smoke test 통과;
- research 통합 흐름(원문 수집→shard→요약→claim→conflict→해결→결론→context 선택) 통과;
- 약 173% 비율의 요약도 보존되고, 10% 초과가 경고 metadata로만 기록되는 것을 확인;
- research 기본 context에 raw source shard가 포함되지 않고, ledger가 허가한 증거만 선택되는 것을 확인;
- work item path traversal, research caller artifact path, raw-content 연구 메시지를 거절하는 것을 확인;
- 번들 안의 `.agent-team-test`, `__pycache__`, 임시 테스트 파일이 없는 것을 확인.

현재 재배포본의 파일 기반 SHA-256은 다음과 같다.

```text
3c599307c2017b8ad0ed2a495085d90a40c23ef5d9bd51dfb85e91c3eaa6f29d
```

이 값은 전달 bundle의 파일만 대상으로 하며, 설치 뒤 생성되는 `.agent-team` runtime state는 포함하지 않는다.

## 현재 범위 밖 또는 후속 구현 항목

다음은 문서에서 설계되어 있지만 지금의 전달 번들이 자동으로 제공하지 않는 영역이다.

- 자연어 Goal을 완전한 Plan IR로 자동 컴파일하고 policy 검증하는 엔진;
- 임의 Plan IR의 dynamic fan-out·join을 자동 생성하는 TaskFlow compiler;
- 역할 선택, work-item 배정, workspace/worktree lease를 완전 자동으로 수행하는 dispatcher;
- 실제 Airflow scheduler 위에서의 Linux/WSL end-to-end DagRun 검증;
- 자동 gate engine, 자동 integration, 자동 release/rollback;
- research plan의 자동 생성과 repository-independent automatic dispatch.

따라서 현재 번들은 “책임 분리, 최소 컨텍스트, 로컬 증거, 좌석/Skill/메시지/연구 control plane”을 실제 파일과 스크립트로 제공한다. 사람 개입이 전혀 없는 범용 개발 조직 전체가 완성되었다고 표현해서는 안 된다.

## 관련 문서

- [저장소 구성과 전달 범위](REPOSITORY_CONFIGURATION.md)
- [아키텍처 문서 인덱스](architecture/INDEX.md)
- [워크플로우 문서 인덱스](workflow/INDEX.md)
- [대량 자료 조사 증거 아키텍처](architecture/16-large-scale-research-evidence.md)
- [대량 자료 조사 루프](workflow/16-large-scale-research-loop.md)
- [전달 번들 스크립트 사용법](output/agent-team-codex-native/scripts/README.md)
- [번들 매니페스트](output/agent-team-codex-native/bundle-manifest.toml)
