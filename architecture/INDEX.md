# Agent Team Architecture Index

## Purpose

This directory is the normative architecture specification for the project-local autonomous development team. It owns static structure: components, boundaries, data ownership, interfaces, authority, deployment topology, and system-wide invariants.

Temporal execution order, state transitions, retries, rework, and exit gates belong to the [Workflow Index](../workflow/INDEX.md).

## Status Labels

| Status | Meaning |
|---|---|
| Implemented | The primary component and validation contract exist in the project. |
| Partial | A working foundation exists, but one or more required interfaces or controllers are missing. |
| Specified | The architecture contract is defined, but its primary implementation is not complete. |

## Document Map

| Order | Document | Architecture ownership | Status |
|---:|---|---|---|
| 00 | [System Context](00-system-context.md) | External actors, system boundary, and top-level dependencies | Partial |
| 01 | [Component Layers](01-component-layers.md) | Layering, dependency direction, and component responsibility | Partial |
| 02 | [Project Layout](02-project-layout.md) | Canonical source, runtime state, and artifact directory structure | Partial |
| 03 | [Domain and State Model](03-domain-and-state-model.md) | Goal, plan, work, loop, message, decision, and evidence entities | Specified |
| 04 | [Team and Authority Model](04-team-and-authority-model.md) | Fixed roles, responsibility, approval authority, and separation of duty | Partial |
| 05 | [Expertise Skill System](05-expertise-skill-system.md) | Project-local skills, catalog, eligibility, and explicit injection | Partial |
| 06 | [Planning and Orchestration](06-planning-and-orchestration.md) | Sequential Thinking, Serena, Plan IR, validation, and TaskFlow | Partial |
| 07 | [Messaging and State Store](07-messaging-and-state-store.md) | SQLite schemas, message envelopes, delivery, snapshots, and outbox | Partial |
| 08 | [Context and Evidence System](08-context-and-evidence-system.md) | Context Compiler, role lenses, Git evidence, and review packets | Partial |
| 09 | [Git and Workspace System](09-git-and-workspace-system.md) | Local bare repositories, branches, worktrees, leases, and write scope | Specified |
| 10 | [Agent Runtime Interfaces](10-agent-runtime-interfaces.md) | Agent activation, runner request and response, and instruction assembly | Partial |
| 11 | [Gate and Evidence Model](11-gate-and-evidence-model.md) | Findings, decisions, OID pinning, gate aggregation, and approval policy | Specified |
| 12 | [Loop Control Model](12-loop-control-model.md) | Module and goal controller hierarchy, durable state, and budgets | Specified |
| 13 | [Runtime Deployment](13-runtime-deployment.md) | Windows host, POSIX Airflow runtime, local IPC, and process topology | Partial |
| 14 | [Security and Authority Boundaries](14-security-and-authority-boundaries.md) | Trust boundaries, permissions, local-only policy, secrets, and privileged actions | Specified |
| 15 | [Observability Data Model](15-observability-data-model.md) | Correlation identifiers, event records, artifacts, audit chain, and retention | Partial |
| 16 | [Large-Scale Research Evidence](16-large-scale-research-evidence.md) | Large-source artifacts, source ledger, shard evidence, conflict routing, and conclusion boundaries | Partial |

## Architecture Versus Workflow

Use this rule when deciding where a statement belongs:

```text
Architecture
= what exists, who owns it, what it stores, and which interfaces connect it

Workflow
= when it runs, in which order, under which state, and what happens next
```

Examples:

| Statement | Owner |
|---|---|
| SQLite owns durable role messages. | Architecture |
| Enqueue a review request after result persistence. | Workflow |
| A skill never grants approval authority. | Architecture |
| Rebind skills after a contract change. | Workflow |
| Review decisions are pinned to a commit OID. | Architecture |
| Route `CHANGES_REQUESTED` to a new revision. | Workflow |

## Core Architecture Invariants

1. The complete system is project-local and does not depend on global agent or skill configuration.
2. Organizational roles own responsibility and authority; skills provide expertise only.
3. Agent processes are disposable; durable state is external.
4. SQLite owns work communication and control state.
5. Local Git owns code and immutable change evidence.
6. The local artifact store owns large generated evidence.
7. Airflow owns execution ordering and infrastructure retry, not technical judgment.
8. Context is compiled per role and action; queue history is never injected wholesale.
9. Concurrent writers use separate branches, worktrees, and build outputs.
10. Gate evidence is invalid when approval, test, and integration OIDs differ.
11. Every control loop has a durable state and explicit budget.
12. Human observation output is not automatically returned to agent context.

## Primary Implemented Components

| Component | Project path |
|---|---|
| MCP dependency installation | `scripts/install_mcp_dependencies.bat` |
| Project setup and MCP configuration | `scripts/setup_agent_team.bat` |
| Serena project and shared-service setup | `skills/serena-project-setup/` |
| Control-plane initializer and verifier | `scripts/init_agent_team.py` |
| Canonical agent team configuration | `agents/` |
| Generated seat identity registry | `agents/seats/registry.toml` |
| Project runtime custom agents | `.codex/agents/` |
| Agent initializer, compiler, and resolver | `scripts/project_agents.py` |
| Skill source packages | `skills/` |
| Skill catalog | `skills/catalog.toml` |
| Project runtime skill mirror | `.codex/skills/` |
| Skill manager | `scripts/project_skills.py` |
| SQLite queue and snapshots | `scripts/agent_team_queue.py` |
| Outbox dispatcher | `scripts/agent_team_dispatcher.py` |
| Context Compiler foundation | `scripts/agent_team_context.py` |
| Human message viewer | `scripts/agent_team_message_viewer.py` |
| Module iteration DAG | `scripts/agent_team_taskflow.py` |

## Change Control

When an architecture contract changes:

1. Update the owning architecture document.
2. Update dependent architecture documents.
3. Update workflows that consume the contract.
4. Update implementation status in both indexes.
5. Add or update executable validation.
6. Record an ADR when the change alters a system boundary, authority boundary, durable schema, or compatibility contract.
