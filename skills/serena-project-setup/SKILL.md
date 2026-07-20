---
name: serena-project-setup
description: Prepare a local project for the optional project-local Serena stdio MCP capability. Use when registering or repairing a Serena project, selecting language support, indexing and health-checking source analysis, initializing Serena memories, or configuring contexts and modes before an agent team starts.
---

# Serena Project Setup

Prepare Serena deliberately before enabling the optional MCP capability. Keep the project setup local; Codex starts Serena through stdio for each MCP client and does not use a shared HTTP service.

## Setup Flow

1. Confirm the target project is the project the agent team will work on.
2. Inspect the installed CLI instead of assuming an option:

   ```powershell
   serena --help
   serena tools list --all
   serena start-mcp-server --help
   ```

3. Create or repair project-local Serena configuration from a normal PowerShell session, outside an active Codex sandbox. Do not change user-level Serena configuration unless the CLI reports an unavoidable prerequisite.

   ```powershell
   Set-Location <target-project>
   serena project create --index       # when .serena/project.yml is absent
   serena project index                # when .serena/project.yml already exists
   serena project health-check
   serena memories initialize
   ```

4. Inspect `.serena/project.yml`. Configure only the languages and workspace folders that belong to the target project. Resolve a failed health check before enabling the team MCP configuration.
5. When a custom context, mode, or prompt override is needed, inspect before creating it:

   ```powershell
   serena context list
   serena mode list
   serena prompts list
   ```

   Create or edit only a project-specific customization with a stated team purpose.

## Stdio MCP Configuration

Do not start an HTTP service, reserve a port, or write an endpoint state file. After the project is ready, let the project setup script generate the Codex MCP entry:

```powershell
python .\scripts\init_agent_team.py --configure-mcp serena
python .\scripts\init_agent_team.py --check-mcp serena
```

The generated entry uses `serena start-mcp-server --project-from-cwd --context codex --transport stdio`. It explicitly sets both `--enable-web-dashboard false` and `--open-web-dashboard false`.

## Knowledge Boundary

After setup, all roles may read targeted Serena memories and perform semantic exploration. The PL alone publishes or refreshes shared project memory; other roles provide concise, evidence-backed proposals through SQLite.

Do not preload every memory, store active task state in Serena memory, or treat MCP runtime state as Serena memory.
