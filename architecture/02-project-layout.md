# Project Layout

## Purpose

Define project-source locations separately from generated runtime state, product workspaces, and evidence artifacts.

## Repository Source Layout

```text
project-root/
  AGENTS.md
  architecture/
    INDEX.md
    00-*.md ... 15-*.md
  workflow/
    INDEX.md
    00-*.md ... 15-*.md
  agents/
    team.toml
    serena-service.toml
    serena-knowledge-policy.toml
    runtime-profiles.toml
    seat-slots.toml
    korean-name-pool.toml
    context-profiles.toml
    roles/
      pm.toml
      pl.toml
      ta.toml
      developer-slot.toml
      qa-sdet.toml
      build-release.toml
    seats/
      registry.toml
  skills/
    catalog.toml
    serena-project-setup/
      SKILL.md
      scripts/
        manage_serena_service.py
    <skill-id>/
      SKILL.md
      agents/
        openai.yaml
  .codex/
    config.toml
    agents/
      <generated-seat-id>.toml
    expertise-catalog.toml
    skills/
      <skill-id>/
        SKILL.md
        agents/
          openai.yaml
  scripts/
    install_mcp_dependencies.bat
    setup_agent_team.bat
    init_agent_team.py
    project_agents.py
    project_skills.py
    agent_team_queue.py
    agent_team_dispatcher.py
    agent_team_context.py
    agent_team_message_viewer.py
    agent_team_taskflow.py
    message_echo_hook.sh
```

## Source Ownership

| Path | Role |
|---|---|
| `architecture/` | Static component, interface, data, authority, and deployment specification |
| `workflow/` | Temporal process, state transition, retry, rework, and gate sequence |
| `agents/` | Canonical team, role, slot, generated identity, and runtime-profile source |
| `agents/context-profiles.toml` | Canonical role/profile context budgets and expansion limits |
| `.codex/config.toml` | Generated project-local Codex MCP and agent-concurrency configuration |
| `.codex/agents/` | Generated self-contained Codex custom-agent runtime mirror |
| `skills/` | Canonical project-local expertise source |
| `.codex/skills/` | Generated project-local runtime mirror |
| `scripts/` | Executable local control-plane foundation |

The canonical agent source is `agents/`. `agents/seats/registry.toml` is generated once by the project initializer and then becomes the durable seat-identity registry. The `.codex/agents/` tree is synchronized output and must not be edited independently.

First installation has no bootstrap skill. A human runs `scripts/install_mcp_dependencies.bat` outside Codex to install the Serena CLI and project-local Sequential Thinking dependency, then runs `scripts/setup_agent_team.bat` outside Codex to create or repair the Serena project, index and health-check it, initialize memories, start the shared loopback Streamable HTTP service, and configure the project-local MCP bindings. The setup batch owns generated `.codex/config.toml` after the service has persisted a concrete endpoint. It configures the eight-agent concurrency cap and generated seat comments; it never runs `serena init` or `serena config edit`.

The canonical skill source is `skills/`. The `.codex/skills/` tree is synchronized output and must not be edited independently.

## Runtime State Layout

Recommended default:

```text
project-root/
  .agent-team/
    state/
      agent-team.db
      serena-service.json
      project-knowledge/
    mcp/
      package.json
      node_modules/
        @modelcontextprotocol/
          server-sequential-thinking/
    npm-cache/
    repositories.json
    repositories/
      product.git
    worktrees/
      W-42-dev-1-r1/
      W-42-ta-review/
      W-42-qa/
      W-42-integration/
    build/
      W-42-dev-1-r1/
      W-42-integration/
    artifacts/
      contexts/
      results/
      builds/
      tests/
      reviews/
      integrations/
      releases/
    logs/
      messages.log
```

`serena-service.json` is the runtime record for the current loopback endpoint. It contains the service-selected random port and is consumed when `.codex/config.toml` is generated. It is runtime state, not source configuration, and a new service port requires only an MCP configuration refresh and Codex reload.

The runtime root may be configured elsewhere on a local disk, but it remains project-scoped and must not create a global configuration dependency.

## Path Rules

- Resolve configured paths to absolute paths at process boundaries.
- Reject path traversal outside the configured project or runtime roots.
- Keep runtime state out of product source commits unless explicitly required.
- Keep build outputs separate by work item and revision.
- Keep review and QA worktrees detached and disposable.
- Never place secrets in versioned workflow, architecture, skill, or repository-registry files.
- Do not use remote URIs for required coordination or artifact storage.

## Repository Registry

```json
{
  "repositories": {
    "product": {
      "bare_repo": ".agent-team/repositories/product.git",
      "default_branch": "integration",
      "index_path": ".agent-team/indexes/product"
    }
  }
}
```

Registry-relative paths resolve from the registry file's directory.

## Cleanup Boundaries

Safe cleanup targets:

- expired disposable worktrees after lease and task checks;
- per-work-item build outputs after artifact promotion;
- retained logs after policy expiration;
- obsolete skill runtime mirrors after catalog change.
- obsolete generated agent runtime files after an explicit seat-identity reset.
- obsolete Sequential Thinking runtime packages after a versioned bootstrap change.

Never clean:

- active workspace leases;
- Git refs required by approval or audit;
- artifacts referenced by open decisions or findings;
- the only recoverable SQLite database copy.

## Current Implementation Status

Partial. Project agent and skill sources, generated Codex runtime mirrors, an idempotent control-plane initializer, scripts, context and result artifact paths, repository registry resolution, and configurable SQLite paths exist. The full workspace allocator and retention controller do not.

## Consumed By

- [Git Workspace Isolation](../workflow/06-git-workspace-isolation.md)
- [Context Compilation](../workflow/08-context-compilation.md)
- [Observability and Audit](../workflow/15-observability-and-audit.md)
