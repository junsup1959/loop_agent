# Agent Team Local Scripts

This directory provides the project-local agent registry, expertise resolver, SQLite message queue, notification dispatcher, Git OID context compiler, human viewer, and Airflow TaskFlow DAG.

## Components

| File | Responsibility |
|---|---|
| `install_mcp_dependencies.bat` | External installer for Serena CLI and the project-local Sequential Thinking dependency |
| `setup_agent_team.bat` | External project setup for core state, Serena service, Codex configuration, and both MCP checks |
| `init_agent_team.py` | Core team initialization, separated MCP dependency/install/configuration operations, URL refresh, and verification |
| `project_agents.py` | Random seat initialization, role/profile validation, Codex agent compilation, and seat-to-skill resolution |
| `project_skills.py` | Project-local expertise catalog validation, synchronization, and role eligibility |
| `agent_team_queue.py` | SQLite durable message queue |
| `agent_team_dispatcher.py` | UDP wake-up and durable outbox polling |
| `agent_team_context.py` | Budgeted role-specific Context Compiler and runner-skill injector |
| `agent_team_message_viewer.py` | Human-only message and Git change viewer |
| `agent_team_taskflow.py` | Airflow TaskFlow module and research iteration DAGs |
| `agent_team_research.py` | Local research ledger, artifacts, source sharding, claims, conflicts, and reference-only context selection |
| `serena_project_knowledge.py` | Serena knowledge-state, evidence, and PL acknowledgement helper |
| `rtk_pre_tool_use.py` | Codex PreToolUse enforcement for the project-local RTK command policy |
| `message_echo_hook.sh` | Human-only echo after a message commit |

## First Installation and Repair

Use two externally executed batch files for first installation or a clean checkout. Run them from a normal PowerShell or Command Prompt outside Codex, with the target project as the current directory. This bundle intentionally has no `AGENTS.md` and no bootstrap skill.

```powershell
Set-Location <target-project>
.\scripts\install_mcp_dependencies.bat --dry-run
.\scripts\install_mcp_dependencies.bat
.\scripts\setup_agent_team.bat --dry-run
.\scripts\setup_agent_team.bat
```

`install_mcp_dependencies.bat` is the dependency phase. It installs the Serena CLI with `uv tool install -p 3.13 serena-agent` only when `serena.exe` is absent, installs the exact Sequential Thinking package into `.agent-team/mcp`, and strictly verifies both dependencies. It never creates `.codex/config.toml`, creates a Serena project, starts a Serena server, or calls `serena init` or `serena config edit`.

`setup_agent_team.bat` is the project configuration phase. It initializes the core control plane, verifies the already-installed MCP dependencies, creates or indexes `.serena/project.yml`, initializes memories, starts the shared loopback HTTP service, configures both MCP entries, strictly checks both entries, and verifies the core control plane. It does not install an MCP dependency; dependency absence is a hard setup failure with a prompt to run the install batch first.

`init_agent_team.py` remains the reusable core/control-plane command. Its plain invocation does not create or index a Serena project, change Serena CLI configuration, start a Serena server, or enable an MCP. Core initialization:

1. verifies exact Python package pins from `scripts/requirements.txt` and installs missing or mismatched dependencies with `pip`;
2. validates native project-local skills without creating a second mirror;
3. generates Korean seat identities only when the registry does not exist;
4. validates role-specific context profiles and compiles eight `.codex/agents/*.toml` files;
5. generates `.codex/config.toml` with the current seat assignment comments, RTK hook, and `agents.max_threads = 8`;
6. initializes `.agent-team/state/agent-team.db` and `.agent-team/state/mcp-capabilities.json`.

Repeated execution is safe and preserves existing seat identities.

Verify the initialized state without writing:

```powershell
python .\scripts\init_agent_team.py --check
```

Use `--json` with either mode for machine-readable output.

Codex loads project configuration only for a trusted project. Trust this project and restart or reload the Codex client after the first initialization so it loads `.codex/config.toml` and discovers the generated seat agents. The generated project config requests `workspace-write` with no additional writable roots and disabled network access; host-managed protected paths can still remain read-only.

## RTK Command Enforcement

The initializer generates one project-local Codex `PreToolUse` hook. It reads `config/agent-team/RTK.md` without adding that document to normal agent context. The hook rewrites supported simple native commands such as `git status` to `rtk git status`; it permits already-prefixed commands and rejects complex commands until they use an explicit `rtk proxy` form. This is project-scoped and does not modify user-global Codex configuration.

The hook applies only after the generated `.codex/config.toml` is trusted and reloaded by Codex. It does not affect a shell outside Codex or an external process that bypasses Codex tool hooks.

If enabled Serena is restarted and receives another random port, do not rerun seat initialization. Refresh only the generated MCP URL, then reload Codex:

```powershell
python .\scripts\init_agent_team.py --refresh-mcp-config
```

## Project MCP Configuration

The initializer writes only a managed project-local `.codex/config.toml` file. It configures:

- eight custom seat agents auto-discovered from `.codex/agents/`;
- `agents.max_threads = 8` and `agents.max_depth = 1`.
- `sandbox_mode = "workspace-write"`, `approval_policy = "on-request"`, no additional writable roots, and disabled network access.

The `serena` and `sequentialthinking` MCP blocks are emitted only after explicit configuration. `setup_agent_team.bat` uses `--configure-mcp`, which refuses to provision a missing dependency; generic manual repair may use `--enable-mcp` when deliberate provisioning is intended. Both generated blocks use `required = false`. Their enabled state and the last known Serena URL are persisted in `.agent-team/state/mcp-capabilities.json`, so ordinary core initialization and `--check` do not probe or fail on MCP availability.

The Serena service manager selects a free loopback port before configuration generation; port `0` is never written to Codex configuration. A spawned role agent connects to that shared endpoint and never starts its own Serena process. The listener is local to one active project and must not be reused for a different project.

All roles receive explicitly injected recommended-tool guidance. They may use targeted Serena semantic exploration and only the named project-memory references selected for their activation when Serena is enabled and available. Tool availability does not grant role authority. The PL alone publishes, refreshes, renames, or deletes shared Serena project memories; other roles submit evidence-backed proposals through SQLite.

The former `mcp-package/` directory is not used. Sequential Thinking is installed by `--install-mcp-dependencies`; `setup_agent_team.bat` configures it only after the strict dependency check. Its absence never blocks a deliberately core-only initialization.

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

- PM, PL, TA, QA/SDET, and Build/Release use `gpt-5.6-terra`.
- The three developer seats use `gpt-5.6-luna`.
- `research-lane` accepts only developer seats, so simple shard reading, extraction, and structured summaries run on Luna. Research planning, synthesis, validation, review, and approval remain bound to their named organizational seats.

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

`config/agent-team/context-profiles.toml` is the authoritative fail-closed budget for every role profile. The compiler never injects a whole thread, repository diff, or skill catalog.

- Messages are limited to the same `thread_id`, `work_item_id`, and target role.
- Git paths are recomputed from the base/head OIDs. Explicit `context_paths` must be present in that authoritative delta.
- Without explicit paths, the compiler prefers paths named by selected messages and only then uses a capped Git-delta fallback.
- Each profile caps messages, message characters, snapshot characters, paths, diff characters, commits, selected skills, selected skill characters, and total packet size. Caller-supplied limits can only lower a cap.
- `selected_skill_ids` requires `actor_seat_id`; the seat resolver validates role eligibility and the runner materializes only those exact `SKILL.md` files.
- A seat-bound activation also materializes exactly `config/agent-team/activation-instructions.md`. The artifact pins its path, SHA-256, and character count; the runner fails closed if it changes. Its characters count toward the packet budget.
- A seat-bound activation also materializes exactly `config/agent-team/recommended-tools.md`. It remains visible to the runner as advisory tool context even when its MCP services are unavailable; the artifact pins its path, SHA-256, and character count, and includes it in the packet budget.
- Evidence artifacts are explicitly selected, UTF-8, hash-pinned, and counted against profile-specific artifact count and character budgets. The compiler preserves full artifacts locally and fails closed when a selected artifact cannot fit; it never truncates evidence implicitly.
- A normal work item may read only `evidence/<work_item_id>/` below the local artifact root. A research iteration accepts no caller-provided artifact path; it resolves its allowed paths from the research ledger for the declared `research_id`.
- TaskFlow requires `actor_seat_id`, so a team agent cannot run without a seat-bound Context Compiler artifact. Direct seat invocations without that artifact must request `NEED_MORE_CONTEXT`.
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

`agent_team_taskflow.py` exposes `agent_team_module_iteration` and `agent_team_research_iteration`. Both use the internal ASCII role key. The research DAG obtains artifact references only through the local research ledger; do not send `artifact_paths` in a research DagRun.

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
  "artifact_root": ".\\agent-team\\artifacts"
}
```

## Large-Source Research

Use `$research-loop` to create the role and evidence contract, then use the local ledger to preserve source provenance and material artifacts. The ledger stores raw source content locally and emits only references, hashes, ratios, and identifiers in its JSON output.

```powershell
$researchDb = ".\.agent-team\state\research.db"
$artifactRoot = ".\.agent-team\artifacts"

python .\scripts\agent_team_research.py `
  --db $researchDb `
  --artifact-root $artifactRoot `
  init

python .\scripts\agent_team_research.py `
  --db $researchDb `
  --artifact-root $artifactRoot `
  create-run `
  --run-id R-001 `
  --title "Storage migration research" `
  --question "Which migration path preserves supported upgrades?"
```

Add an approved local file with `add-file`, or retrieve an approved HTTP(S) source with `add-url`; then use `shard-source`, `record-summary`, `add-claim`, `open-conflict`, `resolve-conflict`, and `finalize`. `record-summary` records the ten-percent and advisory size targets as metadata only. It never truncates or rejects a summary because it exceeds either target.

Use `select-context` to produce reference-only selections. Its `context_compiler_artifact_paths` field is informational for human audit; `agent_team_research_iteration` resolves the same allowed artifact set directly from the ledger, so an arbitrary path cannot be injected by DagRun configuration.

TaskFlow carries only the compiled context artifact path and reference metadata. Immediately before the local runner starts, the Context Compiler hash-verifies and materializes the bounded selected evidence. Research runner messages are checked again: they must contain the active research ID and an artifact, claim, conflict, source, shard, or summary reference, and they are rejected if they contain raw content fields.

Example research DagRun additions:

```json
{
  "research_id": "R-001",
  "research_phase": "CROSS_VALIDATE",
  "research_db_path": ".\\agent-team\\state\\research.db",
  "research_claim_ids": ["CLAIM-17"],
  "research_include_conflicts": true
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
python .\.agents\skills\serena-project-setup\scripts\manage_serena_service.py `
  --service-config .\config\agent-team\serena-service.toml status
```
