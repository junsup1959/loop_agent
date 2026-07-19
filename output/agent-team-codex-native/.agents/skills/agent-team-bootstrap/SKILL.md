---
name: agent-team-bootstrap
description: Install or repair the project-local agent team control plane and optionally enable recommended MCP capabilities. Use for a first installation, after a clean checkout, or when the generated Codex configuration must be rebuilt.
---

# Agent Team Bootstrap

Use this as the first-installation workflow. It initializes the deterministic team control plane first, then enables Serena or Sequential Thinking only when the project wants those recommended capabilities.

## Core Initialization

1. Run the project initializer from a normal PowerShell session, outside an active Codex sandbox. This is the only setup action that creates or updates the project `.codex/` directory.

   ```powershell
   Set-Location <target-project>
   python .\scripts\init_agent_team.py
   ```

   This initializes SQLite, assigns or preserves the eight seats, validates native project-local skills, installs required Python dependencies, and writes `.codex/config.toml`. It does not require an MCP server and creates an empty recommended-capability state by default. It never changes global Codex configuration or relaxes host access policies. Do not run this write-producing initializer through a Codex session that protects `.codex/` as read-only.

2. Verify the core team from the same normal PowerShell session without probing recommended MCP tools.

   ```powershell
   Set-Location <target-project>
   python .\scripts\init_agent_team.py --check
   ```

3. Trust the target project and restart or reload Codex. The new session reads the generated project `.codex/config.toml` and the compiled seat agents; it does not need to write the protected `.codex/` path itself.

## Optional Recommended Capabilities

### Serena

Run `$serena-project-setup` only when semantic source exploration and shared project memories are wanted for this project. It creates or repairs `.serena/project.yml`, indexes the project, initializes Serena memories, and starts one shared loopback Streamable HTTP service.

After that explicit setup succeeds, enable it in Codex:

```powershell
python .\scripts\init_agent_team.py --enable-mcp serena
python .\scripts\init_agent_team.py --check-mcp serena
```

When an enabled Serena service receives a new random port, persist the new endpoint and reload Codex:

```powershell
python .\scripts\init_agent_team.py --refresh-mcp-config
```

### Sequential Thinking

Install and enable Sequential Thinking only when the project wants the optional Plan IR assistance:

```powershell
python .\scripts\init_agent_team.py --enable-mcp sequentialthinking
python .\scripts\init_agent_team.py --check-mcp sequentialthinking
```

To remove either capability from generated Codex configuration without deleting its local data:

```powershell
python .\scripts\init_agent_team.py --disable-mcp serena
python .\scripts\init_agent_team.py --disable-mcp sequentialthinking
```

## Failure Rule

If a recommended capability cannot be enabled, record that capability failure and continue with the initialized core team. The explicitly injected recommended-tool guidance defines the Git, local-search, and explicit-plan fallbacks. Do not treat a missing MCP service as a core team initialization failure.
