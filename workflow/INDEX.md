# Agent Team Workflow Index

## Purpose

This directory is the normative temporal workflow specification for the project-local autonomous development team. It owns execution order, entry and exit conditions, state transitions, retry, rework, and failure routing.

The workflow is designed for local Windows-hosted desktop, offline, native, managed, and device-integrated software. It does not require a remote Git server or remote message broker.

Static components, data models, interfaces, role authority, storage ownership, and runtime topology are defined by the [Architecture Index](../architecture/INDEX.md).

## Status Labels

| Status | Meaning |
|---|---|
| Implemented | The current project contains the primary executable contract and validation for this workflow item. |
| Partial | A usable foundation exists, but one or more required control-plane components are still missing. |
| Specified | The target contract is documented but its primary runtime implementation is not complete. |

## Document Map

| Order | Document | Workflow responsibility | Status |
|---:|---|---|---|
| 00 | [Workflow Overview](00-workflow-overview.md) | End-to-end control flow, boundaries, and system invariants | Partial |
| 01 | [Goal Intake](01-goal-intake.md) | Normalize the objective, scope, constraints, and completion evidence | Specified |
| 02 | [Discovery and Source Evidence](02-discovery-and-source-evidence.md) | Build confidence-labeled repository and solution evidence | Partial |
| 03 | [Plan IR and Task DAG](03-plan-ir-and-task-dag.md) | Decompose the goal, validate dependencies, and compile an execution graph | Specified |
| 04 | [Role and Skill Binding](04-role-and-skill-binding.md) | Bind fixed organizational authority to task-specific expertise | Partial |
| 05 | [TaskFlow Execution](05-taskflow-execution.md) | Execute one immutable plan revision through Airflow TaskFlow | Partial |
| 06 | [Git Workspace Isolation](06-git-workspace-isolation.md) | Allocate branches, worktrees, write scopes, and immutable OIDs | Specified |
| 07 | [Message Routing and Agent Lifecycle](07-message-routing-and-agent-lifecycle.md) | Deliver role messages and activate stateless agent processes | Partial |
| 08 | [Context Compilation](08-context-compilation.md) | Assemble role-specific context from SQLite, Git, skills, and artifacts | Partial |
| 09 | [Agent Task Execution](09-agent-task-execution.md) | Run one bounded agent turn and persist structured results | Partial |
| 10 | [Review, Approval, and Rework](10-review-approval-and-rework.md) | Route evidence through independent gates and revision loops | Specified |
| 11 | [Module Development Loop](11-module-development-loop.md) | Control discovery, design, implementation, verification, and review per module | Partial |
| 12 | [Goal Supervisory Loop](12-goal-supervisory-loop.md) | Coordinate module loops, budgets, replanning, integration, and completion | Specified |
| 13 | [Integration and Release](13-integration-and-release.md) | Integrate approved OIDs and validate installation, upgrade, and rollback | Specified |
| 14 | [Failure and Recovery](14-failure-and-recovery.md) | Distinguish retry, rework, replan, escalation, and blocked outcomes | Partial |
| 15 | [Observability and Audit](15-observability-and-audit.md) | Preserve human-visible and machine-verifiable execution evidence | Partial |
| 16 | [Large-Scale Research Loop](16-large-scale-research-loop.md) | Collect, partition, summarize, cross-validate, and conclude large-source research | Partial |

## Recommended Reading Paths

### End-to-end implementation

Read documents 00 through 16 in numeric order. Each document defines the contract required by the next stage.

Read the [Architecture Index](../architecture/INDEX.md) first when implementing a component or changing a durable interface.

### Runtime operator

Read:

1. [TaskFlow Execution](05-taskflow-execution.md)
2. [Message Routing and Agent Lifecycle](07-message-routing-and-agent-lifecycle.md)
3. [Context Compilation](08-context-compilation.md)
4. [Failure and Recovery](14-failure-and-recovery.md)
5. [Observability and Audit](15-observability-and-audit.md)

### Reviewer or approver

Read:

1. [Role and Skill Binding](04-role-and-skill-binding.md)
2. [Context Compilation](08-context-compilation.md)
3. [Review, Approval, and Rework](10-review-approval-and-rework.md)
4. [Integration and Release](13-integration-and-release.md)

## Architecture Dependencies

Every workflow is constrained by:

- [System Context](../architecture/00-system-context.md);
- [Component Layers](../architecture/01-component-layers.md);
- [Domain and State Model](../architecture/03-domain-and-state-model.md);
- [Team and Authority Model](../architecture/04-team-and-authority-model.md);
- [Security and Authority Boundaries](../architecture/14-security-and-authority-boundaries.md).

Component paths and implementation ownership are maintained in the [Architecture Index](../architecture/INDEX.md), not duplicated here.

## Change Control

When a workflow contract changes:

1. Update the owning numbered document.
2. Update every directly dependent document.
3. Update implementation status in this index.
4. Add or update executable validation.
5. Do not mark a workflow item implemented until its failure path is also tested.
