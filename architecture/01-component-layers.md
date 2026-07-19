# Component Layers

## Purpose

Define component responsibilities and dependency direction so execution logic, durable state, evidence, expertise, and policy do not collapse into one harness.

## Layer Model

```text
Policy and Authority
  - project rules
  - role authority
  - security and release policy

Control
  - goal controller
  - module controller
  - plan validator
  - gate engine

Orchestration
  - Airflow DAGs
  - task adapters
  - retries and scheduling

Execution
  - role agent runner
  - build and test processes
  - integration and release controllers

Context and Expertise
  - skill resolver
  - Context Compiler
  - source-evidence adapters

Persistence and Evidence
  - SQLite
  - local Git
  - artifact store

Observation
  - message viewer
  - human echo
  - audit and operational views
```

## Dependency Direction

Allowed high-level dependencies:

```text
Policy -> constrains all lower layers
Control -> invokes orchestration through contracts
Orchestration -> invokes execution tasks
Execution -> consumes context and writes evidence
Context -> reads persistence and skill packages
Observation -> reads durable state and evidence
```

Disallowed dependencies:

- observation output mutating control state;
- a skill granting role authority;
- Airflow deciding architecture approval;
- an agent runner mutating Plan IR topology;
- SQLite messages embedding source code as the authoritative copy;
- Context Compiler accepting agent-provided changed paths without Git verification.

## Component Responsibility Matrix

| Component | Owns | Does not own |
|---|---|---|
| Goal controller | Goal state, workstreams, budgets, completion | Module implementation |
| Module controller | Module phase, iteration, routing | Goal-wide priority |
| Plan validator | Plan structure, dependency, ownership, scope checks | Code meaning |
| Airflow | Task order, DagRun, infrastructure retry | Technical approval |
| Skill resolver | Eligibility and project-local skill paths | Role assignment authority |
| Agent runner | One bounded role turn | Durable team memory |
| SQLite queue | Role messages, delivery, snapshot state | Code or large artifacts |
| Context Compiler | Role-specific evidence packet | Final design judgment |
| Local Git | Source and immutable OIDs | Work approval state |
| Gate engine | Structured gate aggregation | Implementing fixes |
| Artifact store | Large generated evidence | Workflow state transitions |
| Human viewer | Read-only local observation | Agent context generation |

## Interface Style

Components exchange:

- stable IDs;
- immutable Git OIDs;
- compact JSON or TOML contracts;
- local artifact references;
- explicit status values;
- bounded error classifications.

Components do not exchange:

- implicit in-memory ownership across process restarts;
- unbounded conversation transcripts;
- ambiguous branch names without OIDs;
- approval text without a structured decision record.

## Current Implementation Mapping

| Layer | Implemented foundation |
|---|---|
| Policy and authority | `AGENTS.md`, skill authority boundaries |
| Control | Not complete |
| Orchestration | `scripts/agent_team_taskflow.py` |
| Execution | subprocess runner adapter in TaskFlow script |
| Context and expertise | `scripts/project_skills.py`, `scripts/agent_team_context.py` |
| Persistence and evidence | SQLite queue, local Git reader, JSON artifacts |
| Observation | dispatcher stdout, shell echo, message viewer |

## Current Implementation Status

Partial. The dependency boundaries are documented and several lower-layer components exist, but the control and gate layers remain specified.

## Consumed By

- [TaskFlow Execution](../workflow/05-taskflow-execution.md)
- [Agent Task Execution](../workflow/09-agent-task-execution.md)
- [Observability and Audit](../workflow/15-observability-and-audit.md)
