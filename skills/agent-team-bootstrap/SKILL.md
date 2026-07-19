---
name: agent-team-bootstrap
description: Install or repair the complete project-local agent team in its required order. Use for a first installation, after a clean checkout, or when the Serena shared-service endpoint and generated Codex configuration must be rebuilt before agents are activated.
---

# Agent Team Bootstrap

Use this as the only first-installation workflow. It composes the Serena setup workflow with the deterministic team initializer; it does not duplicate Serena configuration inside the initializer.

## Required Order

1. Run `$serena-project-setup` for the target project.
   - Create or repair `.serena/project.yml`.
   - Set the actual project languages and workspace folders.
   - Index the project and resolve failed health checks.
   - Initialize Serena memories.
   - Start the shared loopback Streamable HTTP service with a randomly selected persisted port.

2. Confirm the persisted service is reachable.

   ```powershell
   python .\skills\serena-project-setup\scripts\manage_serena_service.py `
     --service-config .\agents\serena-service.toml status
   ```

3. Run the project initializer exactly once after Serena setup succeeds.

   ```powershell
   python .\scripts\init_agent_team.py
   ```

   This installs checked Python and npm dependencies, initializes SQLite, assigns or preserves the eight seats, mirrors project-local skills, and writes `.codex/config.toml` with the persisted HTTP endpoint.

4. Restart or reload Codex after the generated MCP URL changes, then verify without mutation.

   ```powershell
   python .\scripts\init_agent_team.py --check
   ```

## Endpoint Rotation

When the shared service is restarted and receives a new random port, run only:

```powershell
python .\scripts\init_agent_team.py --refresh-mcp-config
```

Then reload Codex. Do not rerun seat generation or write a global Codex configuration.

## Failure Rule

If Serena setup, indexing, health-check, or service reachability fails, stop before `init_agent_team.py`. Fix the Serena setup through `$serena-project-setup`, then resume this skill.
