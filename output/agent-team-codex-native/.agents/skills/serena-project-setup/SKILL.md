---
name: serena-project-setup
description: Prepare a local project for the optional shared Serena Streamable HTTP MCP capability. Use when registering or repairing a Serena project, selecting language support, indexing and health-checking source analysis, initializing Serena memories, configuring contexts or modes, or starting or rotating the project-local shared Serena service endpoint.
---

# Serena Project Setup

Configure Serena deliberately when the project enables this recommended capability. Keep all agents on one local project and one shared Streamable HTTP server; do not let each spawned agent start a separate stdio server.

## Setup Flow

1. Confirm the target is the one project all concurrent agents will work on. A shared Streamable HTTP Serena server has one active project.
2. Inspect the installed CLI instead of assuming an option:

   ```powershell
   serena --help
   serena tools list --all
   serena start-mcp-server --help
   ```

3. When the Serena CLI global configuration is absent, initialize the CLI once. This is a Serena CLI prerequisite, not a Codex configuration; keep the team and MCP configuration project-local.

   ```powershell
   serena init --language-backend LSP
   ```

4. Create or repair project-local Serena configuration. Use the CLI first; use `serena config edit` only when a deliberate global CLI setting is required.

   ```powershell
   Set-Location <target-project>
   serena project create --index       # only when .serena/project.yml is absent
   serena project index                # refresh an existing project index
   serena project health-check
   serena memories initialize
   ```

5. Inspect `.serena/project.yml`. Configure only the languages and workspace folders that belong to the target project. Resolve a failed health check before starting the team.
6. When a custom context, mode, or prompt override is actually needed, inspect before creating it:

   ```powershell
   serena context list
   serena mode list
   serena prompts list
   ```

   Create or edit only a project-specific customization that has a stated team purpose. Do not create speculative overrides.

## Shared HTTP Service

Use `config/agent-team/serena-service.toml` as the project-local service contract. Start the server through the bundled service manager so it chooses an available loopback port, persists the endpoint in `.agent-team/state/serena-service.json`, and prevents agent spawn from creating a new MCP process.

```powershell
python .\.agents\skills\serena-project-setup\scripts\manage_serena_service.py `
  --service-config .\config\agent-team\serena-service.toml start `
  --project <target-project>
```

The command repeats the Serena health check, selects a random available port, and persists both the concrete endpoint and its successful health record. It refuses to start on a failed health check. After this explicit setup succeeds, run `python .\scripts\init_agent_team.py --enable-mcp serena` to add the optional MCP configuration.

Use the state file to verify the endpoint before activating agents:

```powershell
python .\.agents\skills\serena-project-setup\scripts\manage_serena_service.py `
  --service-config .\config\agent-team\serena-service.toml status
```

Do not use port `0` directly in Codex configuration. The manager resolves a random port first, records the concrete URL, then the configuration generator consumes that URL.

## Knowledge Boundary

After setup, all roles may read targeted Serena memories and perform semantic exploration. The PL alone publishes or refreshes shared project memory; other roles provide concise, evidence-backed proposals through SQLite.

Do not preload every memory, store active task state in Serena memory, expose the HTTP listener beyond loopback, or use one shared server for different active projects.
