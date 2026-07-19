# Agent Team Local Scripts

This directory provides the project-local agent registry, expertise resolver, SQLite message queue, notification dispatcher, Git OID context compiler, human viewer, and Airflow TaskFlow DAG.

## Components

| File | Responsibility |
|---|---|
| `init_agent_team.py` | Idempotent post-Serena team initialization, MCP URL refresh, and non-mutating verification |
| `project_agents.py` | Random seat initialization, role/profile validation, Codex agent compilation, and seat-to-skill resolution |
| `project_skills.py` | Project-local expertise catalog validation, synchronization, and role eligibility |
| `agent_team_queue.py` | SQLite durable message queue |
| `agent_team_dispatcher.py` | UDP wake-up and durable outbox polling |
| `agent_team_context.py` | Budgeted role-specific Context Compiler and runner-skill injector |
| `agent_team_message_viewer.py` | Human-only message and Git change viewer |
| `agent_team_taskflow.py` | Airflow TaskFlow module iteration DAG |
| `serena_project_knowledge.py` | Serena knowledge-state, evidence, and PL acknowledgement helper |
| `message_echo_hook.sh` | Human-only echo after a message commit |

## First Installation and Repair

Use the project-local `$agent-team-bootstrap` skill for a first installation, a clean checkout, or a Serena service repair. It is the only workflow that composes Serena setup with agent-team initialization. Before the normal runtime skill mirror exists, `AGENTS.md` directs the activation to the canonical source at `skills/agent-team-bootstrap/SKILL.md`.

`$agent-team-bootstrap` performs this required order:

1. invokes `$serena-project-setup` for this one project;
2. creates or repairs `.serena/project.yml`, configures the actual project languages and workspace folders, indexes the project, resolves health-check failures, and initializes Serena memories;
3. starts one shared loopback Serena Streamable HTTP service using `agents/serena-service.toml`;
4. persists its randomly selected concrete endpoint in `.agent-team/state/serena-service.json`;
5. invokes `python .\scripts\init_agent_team.py` only after that service is reachable.

`init_agent_team.py` does not run `serena init`, create or index a Serena project, change Serena CLI configuration, or start a Serena server. It requires the completed Serena setup and then:

1. verifies exact Python package pins from `scripts/requirements.txt` and installs missing or mismatched dependencies with `pip`;
2. installs the pinned Sequential Thinking MCP package with `npm install` under `.agent-team/mcp`;
3. validates and synchronizes all project-local skills;
4. generates Korean seat identities only when the registry does not exist;
5. validates role-specific context profiles and compiles eight `.codex/agents/*.toml` files;
6. generates `.codex/config.toml` with the persisted Serena HTTP endpoint, Sequential Thinking, the current seat assignment comments, and `agents.max_threads = 8`;
7. initializes `.agent-team/state/agent-team.db`.

Repeated execution is safe and preserves existing seat identities.

Verify the initialized state without writing:

```powershell
python .\scripts\init_agent_team.py --check
```

Use `--json` with either mode for machine-readable output.

Codex loads project configuration only for a trusted project. Trust this project and restart or reload the Codex client after the first initialization so it loads `.codex/config.toml` and discovers the generated seat agents.

If the Serena service is restarted and receives another random port, do not rerun seat initialization. Refresh only the generated MCP URL, then reload Codex:

```powershell
python .\scripts\init_agent_team.py --refresh-mcp-config
```

## Project MCP Configuration

The initializer writes only a managed project-local `.codex/config.toml` file. It configures:

- `serena`, one shared `streamable-http` endpoint at `http://127.0.0.1:<persisted-port>/mcp` read from `.agent-team/state/serena-service.json`;
- `sequentialthinking`, started by Node from `.agent-team/mcp/node_modules/`;
- eight custom seat agents auto-discovered from `.codex/agents/`;
- `agents.max_threads = 8` and `agents.max_depth = 1`.

The Serena service manager selects a free loopback port before configuration generation; port `0` is never written to Codex configuration. A spawned role agent connects to that shared endpoint and never starts its own Serena process. The listener is local to one active project and must not be reused for a different project.

All roles may use targeted Serena semantic exploration and only the named project-memory references selected for their activation. Tool availability does not grant role authority. The PL alone publishes, refreshes, renames, or deletes shared Serena project memories; other roles submit evidence-backed proposals through SQLite.

The former `mcp-package/` directory is not used. Sequential Thinking is installed by the initializer with the pinned npm package version and checked before Codex configuration is accepted.

## Project Agent Seats

Initialize eight durable Korean-named seat identities once:

```powershell
python .\scripts\project_agents.py init
```

The command randomly selects unique names and persists them in `agents/seats/registry.toml`. Repeated `init` calls keep the existing identities.

Replacing identities is an explicit destructive control-plane operation because queued messages and artifacts may refer to existing seat IDs:

```powershell
python .\scripts\project_agents.py regenerate --confirm-identity-reset
```

Validate and compile the canonical configuration:

```powershell
python .\scripts\project_agents.py validate
python .\scripts\project_agents.py sync
python .\scripts\project_agents.py list
```

`sync` creates eight self-contained project agents under `.codex/agents/`. Models are pinned by profile:

- PM, PL, and TA use `gpt-5.6-sol`.
- The three developer seats, QA/SDET, and Build/Release use `gpt-5.6-terra`.

Resolve one seat with an explicit skill packet:

```powershell
$developerSeat = (
  python .\scripts\project_agents.py list |
    ConvertFrom-Json |
    Where-Object role_key -eq "dev_1"
).seat_id

python .\scripts\project_agents.py resolve `
  --seat $developerSeat `
  --skill map-codebase `
  --skill engineer-dotnet-desktop `
  --skill engineer-test-coverage
```

## Python Dependencies

Installing only `apache-airflow-task-sdk` is enough to author DAGs through `airflow.sdk` and use Task SDK interfaces. It does not provide the Scheduler and API Server required for a real Airflow deployment.

The project `requirements.txt` installs the complete Airflow runtime. `apache-airflow` installs the compatible Task SDK, so the Task SDK is not listed a second time.

After `$serena-project-setup` succeeds, `init_agent_team.py` performs this dependency check automatically. It calls `pip install` only when an exact requirement pin is missing or differs from the installed version.

Example for a supported Python version:

```powershell
python -m pip install `
  -r .\scripts\requirements.txt `
  --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.3.0/constraints-3.14.txt
```

Use the constraint file that matches the supported Python minor version. Airflow does not support native Windows execution. Run the Airflow runtime in WSL2 or a local Linux container. The SQLite queue and human viewer can continue to run under native Windows Python.

## Bounded Context Injection

`agents/context-profiles.toml` is the authoritative fail-closed budget for every role profile. The compiler never injects a whole thread, repository diff, or skill catalog.

- Messages are limited to the same `thread_id`, `work_item_id`, and target role.
- Git paths are recomputed from the base/head OIDs. Explicit `context_paths` must be present in that authoritative delta.
- Without explicit paths, the compiler prefers paths named by selected messages and only then uses a capped Git-delta fallback.
- Each profile caps messages, message characters, snapshot characters, paths, diff characters, commits, selected skills, selected skill characters, and total packet size. Caller-supplied limits can only lower a cap.
- `selected_skill_ids` requires `actor_seat_id`; the seat resolver validates role eligibility and the runner materializes only those exact `SKILL.md` files.
- Any excluded or truncated material is recorded in `omitted_context`. The correct response is an explicit `NEED_MORE_CONTEXT` request, not an implicit expansion.

Compile a bounded packet directly:

```powershell
python .\scripts\agent_team_context.py `
  --db .\.agent-team\state\agent-team.db `
  --registry .\.agent-team\repositories.json `
  --thread thread-W42 `
  --work-item W-42 `
  --role dev_1 `
  --seat DEV_<generated-name> `
  --repo-id product `
  --base-oid 71ae234 `
  --head-oid d920f31 `
  --profile implementation `
  --action "Implement the assigned state transition" `
  --path src/runtime/state_manager.cpp `
  --skill engineer-cpp-systems
```

## Queue Initialization

```powershell
python .\scripts\agent_team_queue.py `
  --db .\.agent-team\state\agent-team.db `
  init
```

## Message Persistence

When Git Bash `sh.exe` is available on `PATH`, enable the human echo hook:

```powershell
$payload = '{"repo_id":"product","base_oid":"71ae234","head_oid":"d920f31"}'

python .\scripts\agent_team_queue.py `
  --db .\.agent-team\state\agent-team.db `
  --echo-script .\scripts\message_echo_hook.sh `
  enqueue `
  --thread thread-W42 `
  --work-item W-42 `
  --from-role dev_1 `
  --to-role ta `
  --type REVIEW_REQUEST `
  --dedupe-key W-42:review:d920f31 `
  --payload $payload
```

The hook output is visible to humans only. It is not written back to SQLite, a context snapshot, or an agent input.

Add a durable human log when required:

```powershell
--message-log .\.agent-team\logs\messages.log
```

## Dispatcher

Drain the outbox once:

```powershell
python .\scripts\agent_team_dispatcher.py `
  --db .\.agent-team\state\agent-team.db `
  --once
```

Run UDP wake-up with polling fallback:

```powershell
python .\scripts\agent_team_dispatcher.py `
  --db .\.agent-team\state\agent-team.db
```

## Human Message Viewer

Show every durable agent-to-agent message in chronological order:

```powershell
python .\scripts\agent_team_message_viewer.py
```

No database path, thread ID, message ID, or other selector is required. The command reads the project default database at `.agent-team/state/agent-team.db`; it reports a clear error instead of creating a new empty database when that file does not exist.

Repository registry example:

```json
{
  "repositories": {
    "product": {
      "bare_repo": "C:\\agent-team\\repositories\\product.git",
      "default_branch": "integration"
    }
  }
}
```

Optional: resolve the Git change attached to one message:

```powershell
python .\scripts\agent_team_message_viewer.py `
  --registry .\.agent-team\repositories.json `
  --message-id msg-1024 `
  --show-diff
```

Watch new messages for the TA role:

```powershell
python .\scripts\agent_team_message_viewer.py `
  --registry .\.agent-team\repositories.json `
  --role ta `
  --watch-seconds 1
```

Viewer output is observation-only and consumes no agent context tokens.

## TaskFlow DAG

`agent_team_taskflow.py` exposes the `agent_team_module_iteration` DAG. A DagRun configuration currently uses the internal ASCII role key:

```json
{
  "goal_id": "G-001",
  "work_item_id": "W-42",
  "thread_id": "thread-W42",
  "iteration": 3,
  "actor_role": "dev_1",
  "actor_seat_id": "DEV_<generated-name>",
  "repo_id": "product",
  "base_oid": "71ae234",
  "head_oid": "d920f31",
  "context_profile": "implementation",
  "context_action": "Implement the approved state transition",
  "context_paths": ["src/runtime/state_manager.cpp"],
  "selected_skill_ids": ["engineer-cpp-systems"],
  "db_path": "C:\\agent-team\\state\\agent-team.db",
  "registry_path": "C:\\agent-team\\repositories.json",
  "artifact_root": "C:\\agent-team\\artifacts"
}
```

Configure the Agent Runner through a JSON array environment variable:

```powershell
$env:AGENT_TEAM_RUNNER_COMMAND_JSON='["python","C:\\agent-team\\runner.py"]'
```

The runner reads one context request from standard input and returns one JSON object on standard output.

## Validation

```powershell
python .\scripts\init_agent_team.py --check
```

For first-installation or endpoint troubleshooting, verify the shared service before this check:

```powershell
python .\skills\serena-project-setup\scripts\manage_serena_service.py `
  --service-config .\agents\serena-service.toml status
```
