# System Context

## Purpose

Define the autonomous development team as one project-local system and identify the actors, services, storage boundaries, and prohibited external dependencies around it.

## System Boundary

```text
Human Project Owner
        |
        v
Project-Local Agent Team System
  - Goal and Plan Control
  - Fixed Organizational Roles
  - Dynamic Expertise Skills
  - TaskFlow Execution
  - SQLite Communication and State
  - Context Compilation
  - Local Git Workspaces
  - Local Evidence and Artifacts
        |
        v
Local Product Repositories and Build Toolchains
```

## External Actors

| Actor | Interaction |
|---|---|
| Human project owner | Defines goals, authority boundaries, local policies, and exceptional approvals. |
| Local operating system | Provides files, processes, IPC, credentials, devices, and native toolchains. |
| Local product repository | Supplies source, history, branches, worktrees, and integration refs. |
| Language and build toolchains | Compile, test, package, analyze, and inspect the product. |
| Model runtime | Executes bounded role tasks using compiled project context. |
| MCP providers | Supply structured planning and targeted semantic source evidence under project policy. |

## Internal Subsystems

```text
Goal Control
  -> Planning and Plan IR
  -> Role and Skill Binding
  -> TaskFlow Orchestration
  -> Agent Runtime

SQLite State <-> Dispatcher <-> Agent Activation
Local Git <-> Context Compiler <-> Artifact Store
Gate Engine <-> Module Controller <-> Goal Controller
```

## Owned Data

| Data | System of record |
|---|---|
| Goal, plan, work, loop, finding, and decision state | SQLite control schema |
| Role messages and delivery state | SQLite message schema |
| Source code and commit history | Local Git |
| Build, test, context, review, and release evidence | Local artifact store |
| Skill definitions and eligibility | Project source and project runtime mirror |
| DAG and task execution records | Airflow metadata plus project correlation IDs |

## Explicitly External or Optional

- Model execution may be local or remote according to a separate runtime policy.
- MCP may run through a project-configured local package or local container.
- Language servers and semantic analyzers are adapters, not systems of record.
- Airflow metadata is execution history, not the authoritative work-state database.

## Prohibited Dependencies

- remote Git hosting as a required coordination mechanism;
- SaaS message brokers;
- external artifact storage;
- globally installed agent definitions as a runtime dependency;
- unrestricted agent-to-agent free-form conversation;
- shared mutable workspaces between concurrent writers.

## Availability Model

The system is restartable rather than continuously resident:

- SQLite restores control and communication state.
- Git restores source state.
- artifacts restore generated evidence.
- TaskFlow restores task execution state.
- agent processes may terminate after each bounded turn.

## Architecture Consequences

- Every durable handoff requires identifiers and persisted evidence.
- Wake-up mechanisms may fail without losing messages.
- Context must be reconstructed from systems of record.
- A missing model process does not erase team state.
- Local filesystem integrity and backup policy become important operational dependencies.

## Current Implementation Status

Partial. Skills, queueing, dispatch, context reconstruction, human observation, and one TaskFlow DAG exist. Goal control, role registry, workspaces, gate engine, and supervisory controllers are specified but not implemented.

## Consumed By

- [Workflow Overview](../workflow/00-workflow-overview.md)
- [Goal Supervisory Loop](../workflow/12-goal-supervisory-loop.md)
- [Integration and Release](../workflow/13-integration-and-release.md)
