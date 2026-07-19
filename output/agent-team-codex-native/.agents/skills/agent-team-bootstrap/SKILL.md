---
name: agent-team-bootstrap
description: Install or repair the project-local agent team control plane and optionally enable recommended MCP capabilities. Use for a first installation, after a clean checkout, or when the generated Codex configuration must be rebuilt.
---

# Agent Team Bootstrap

Use this as the first-installation workflow. It initializes the deterministic team control plane first, then enables Serena or Sequential Thinking only when the project wants those recommended capabilities.

## Core Initialization

1. Run the project initializer.

   ```powershell
   python .\scripts\init_agent_team.py
   ```

   This initializes SQLite, assigns or preserves the eight seats, validates native project-local skills, installs required Python dependencies, and writes `.codex/config.toml`. It does not require an MCP server and creates an empty recommended-capability state by default.

2. Restart or reload Codex, then verify the core team without probing recommended MCP tools.

   ```powershell
   python .\scripts\init_agent_team.py --check
   ```

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
