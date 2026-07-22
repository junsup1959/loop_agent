---
name: serena-project-setup
version: 2.0.0
description: Run the mandatory PL-owned Serena onboarding and refresh lifecycle for project-local, slow-changing target-project knowledge before developer implementation or rework.
---

# Serena Project Setup 2.0.0

Use this Skill only under the PL capability. It prepares the project-local Serena
stdio MCP and publishes the minimum source-OID-pinned project knowledge required
by an implementation or rework transition. Other capabilities may read the named
bindings but must not publish or refresh shared memory.

## Mandatory PL Onboarding Lifecycle

1. Confirm the exact registered repository and full source OID. Read
   `agents/serena-memory-boundary.md` before inspecting proposed memory content.
2. Verify `initial_instructions` is available and invoke it. Preserve a SHA-256
   evidence receipt; absence, non-use, or invalid evidence fails onboarding.
3. Inspect the installed CLI rather than assuming an option:

   ```powershell
   serena --help
   serena tools list --all
   serena start-mcp-server --help
   ```

4. Create or repair project-local Serena configuration from a normal PowerShell
   session outside an active Codex sandbox. Do not change user-level Serena
   configuration unless the CLI reports an unavoidable prerequisite.

   ```powershell
   Set-Location <target-project>
   serena project create --index       # when .serena/project.yml is absent
   serena project index                # when .serena/project.yml already exists
   serena project health-check
   serena memories initialize
   ```

5. Inspect `.serena/project.yml`. Configure only languages and workspace folders
   belonging to the target repository. Resolve health-check failures first.
6. Create or refresh the five canonical memories only as needed: `core`,
   `tech_stack`, `suggested_commands`, `conventions`, and `task_completion`.
   A new repository, missing required memory, stale knowledge, or material
   project/configuration change requires refresh. Unchanged fresh bytes reuse the
   existing snapshot.
7. Call `ensure_serena_onboarding(repo, evidence, required_memories)` with PL
   capability evidence, exact source OID, policy digest, named refs, content
   digests, and the transition-specific minimum from
   `agents/serena-knowledge-policy.toml`. Persist the returned snapshot before
   issuing the developer handoff.
8. When a custom context or mode is required, inspect before creating it:

   ```powershell
   serena context list
   serena mode list
   serena prompts list
   ```

   Create only project-specific customizations. Prompt text is not shared memory.

## Stdio MCP Configuration

Do not start an HTTP service, reserve a port, or write an endpoint state file. After the project is ready, let the project setup script generate the Codex MCP entry:

```powershell
python .\scripts\init_agent_team.py --configure-mcp serena
python .\scripts\init_agent_team.py --check-mcp serena
```

The generated entry uses `serena start-mcp-server --project-from-cwd --context codex --transport stdio`. It explicitly sets both `--enable-web-dashboard false` and `--open-web-dashboard false`.

## Knowledge Boundary

Before initial onboarding or a shared-memory refresh, read `agents/serena-memory-boundary.md` in canonical source or `config/agent-team/serena-memory-boundary.md` in a generated bundle. After setup, all roles may read targeted Serena memories and perform semantic exploration. The PL alone publishes or refreshes shared project memory; other roles provide concise, evidence-backed proposals through SQLite.

Publish only stable project structure, technology, conventions, coding guidance,
and build/test commands supported by repository evidence. Reject active work
items, current diffs, Git/OID approvals, leases, team roles, workflows,
activation contracts, prompts, and per-run results. Those belong in SQLite, Git,
or activation artifacts.

Bind only the transition-specific named refs. Wildcards, duplicate or missing
names, `docs/` references, changed bytes, invalid SHA-256 values, or source/policy
digest mismatches fail closed.

Before the first developer source mutation, the developer must read every named
binding in the accepted contract. The TaskFlow preflight records the matching
Serena consumption receipts in the v4 StateStore. Result receipts and required
MCP usage receipts must match the same contract bindings; there is no fallback.
