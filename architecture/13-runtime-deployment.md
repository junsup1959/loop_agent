# Runtime Deployment

## Purpose

Define the local process and operating-system topology for a Windows-hosted project with a supported POSIX Airflow runtime.

## Deployment Topology

```text
Windows Host
  - project source
  - local bare Git repositories
  - worktrees and build outputs
  - SQLite state database
  - skill manager
  - Context Compiler
  - message viewer
  - human echo
  - product toolchains and devices

Local POSIX Runtime
  - Airflow scheduler
  - Airflow API server
  - TaskFlow workers
  - shared project and runtime paths

Project-Local MCP Services
  - Sequential Thinking provider from .agent-team/mcp
  - one Serena Streamable HTTP service from the Serena CLI
  - loopback-only endpoint with a persisted random port
```

The POSIX runtime may be WSL2 or a local Linux container. It remains local to the project environment.

## Project Installation and Configuration

First installation is executed by a human from a normal terminal outside Codex, not by a bootstrap skill:

1. run `scripts/install_mcp_dependencies.bat` to ensure the Serena CLI and the pinned Sequential Thinking package under `.agent-team/mcp` are installed and strictly verified;
2. run `scripts/setup_agent_team.bat` to initialize the core control plane, create or repair the Serena project, select its languages and workspace folders, index it, resolve health-check failures, initialize Serena memories, and start one shared Streamable HTTP service on an available `127.0.0.1` port;
3. configure both already-installed MCP dependencies, persist the concrete Serena endpoint in `.agent-team/state/serena-service.json`, strictly check both MCP endpoints, and verify the generated core control plane.

`scripts/init_agent_team.py` supplies the separated dependency install/check and MCP configuration operations. The ordinary core initializer synchronizes project-local skills and custom seat agents, initializes SQLite, and writes `.codex/config.toml`; it does not initialize Serena, alter Serena CLI settings, or start a Serena process.

The generated Codex configuration connects Serena through its persisted Streamable HTTP URL, for example `http://127.0.0.1:<port>/mcp`. It does not contain a Serena stdio command, arguments, or tool allow-list. The shared service is used only by agents assigned to the same active project. Sequential Thinking continues to start through the exact Node package entrypoint installed beneath the configured project-local runtime root. Project trust and a Codex restart or reload are required before a client loads a changed `.codex/config.toml`.

## Process Roles

| Process | Lifetime |
|---|---|
| Airflow services | Long-running local service |
| Serena shared HTTP service | Long-running project-scoped local service |
| Outbox dispatcher | Long-running or periodic process |
| Agent runner | Per bounded task |
| Context Compiler | Per context request |
| Message viewer | On demand or watch process |
| Human echo hook | Per committed message |
| Semantic analyzer | On demand or cached local service |
| Product build and test | Per task or gate |

## Local IPC

Current wake-up:

- UDP loopback;
- configurable host and port;
- debounce;
- polling fallback.

Future Windows-native wake-up may use a named pipe, but SQLite remains the durable authority.

## Shared Filesystem Requirements

The Airflow runtime must access:

- Dag source;
- SQLite database or a safe mediated state interface;
- repository registry;
- local bare repositories;
- context and result artifact roots;
- runner executable;
- allocated worktrees.

Path translation between Windows and POSIX environments must be explicit. Do not rely on ambiguous shell path conversion.

## Configuration Surfaces

Current environment variables:

```text
AGENT_TEAM_RUNNER_COMMAND_JSON
AGENT_TEAM_WAKE_HOST
AGENT_TEAM_WAKE_PORT
AGENT_TEAM_MESSAGE_ECHO
AGENT_TEAM_MESSAGE_ECHO_SCRIPT
AGENT_TEAM_MESSAGE_LOG
AGENT_TEAM_SH
```

Configuration values are project deployment settings, not global expertise or role definitions.

## Concurrency

- SQLite uses WAL and busy timeout.
- Concurrent writers use isolated worktrees.
- Airflow concurrency must respect workspace and build leases.
- Dispatcher publication is idempotent.
- Model runner concurrency must remain within the configured team and resource limits.

## Availability and Restart

After process restart:

- Airflow restores execution metadata;
- SQLite restores messages and state;
- expired leases are reconciled;
- outbox polling restores missed wake-ups;
- Git and artifacts restore evidence;
- agent processes are recreated as needed.
- Serena service state records the last endpoint, but service restart or port rotation requires an endpoint check, `init_agent_team.py --refresh-mcp-config`, and a Codex reload before agents are activated.

## Current Implementation Status

Partial. Native Windows scripts, project-local storage, skill-driven Serena bootstrap, persisted loopback HTTP endpoint configuration, pinned npm and Python dependency checks, UDP wake and polling, Airflow TaskFlow import, and POSIX runtime guidance exist. Productionized Airflow deployment, path translation, shared-state locking, and full operational service supervision are not implemented.

## Consumed By

- [TaskFlow Execution](../workflow/05-taskflow-execution.md)
- [Message Routing and Agent Lifecycle](../workflow/07-message-routing-and-agent-lifecycle.md)
- [Observability and Audit](../workflow/15-observability-and-audit.md)
