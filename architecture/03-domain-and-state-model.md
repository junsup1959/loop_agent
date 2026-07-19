# Domain and State Model

## Purpose

Define durable business and control entities independently of any model process or Airflow DagRun.

## Entity Hierarchy

```text
Goal
  -> Plan Revision
  -> Workstream
  -> Module Loop
  -> Work Item
  -> Work-Item Revision
  -> Agent Turn
  -> Artifact and Evidence

Cross-cutting:
  Thread
  Message
  Snapshot
  Workspace Lease
  Finding
  Gate Decision
  Integration
  Release
```

## Core Entities

### Goal

Owns:

- objective;
- scope and non-goals;
- acceptance criteria;
- authority boundary;
- total budget;
- active Plan IR revision;
- terminal status.

### Plan Revision

Owns:

- immutable task graph;
- assumptions and constraints;
- work-item ownership;
- dependencies and write scopes;
- required skills and gates;
- failure routes and budgets.

### Workstream

Groups independently coordinated development areas under one goal.

### Module Loop

Owns the durable phase and iteration state for one module or bounded behavior.

### Work Item

Owns one accountable objective, read and write scopes, input and output contracts, reviewer routing, and completion evidence.

### Work-Item Revision

Pins:

- assigned role;
- selected skill IDs;
- base OID;
- submitted head OID;
- workspace lease;
- unresolved findings;
- iteration budget.

### Agent Turn

Represents one bounded activation with one context artifact, one runner request, one result artifact, and zero or more outgoing messages.

## Communication Entities

### Thread

Groups role communication around one work item or decision boundary.

### Message

Stores sender, recipient, type, priority, payload, delivery state, lease, attempts, and deduplication key.

### Snapshot

Stores a role-specific projection of thread state through a known message sequence.

## Evidence Entities

### Artifact

Names a local generated object with producer, content type, target OID, integrity data, and retention state.

### Finding

Stores an evidence-backed issue, severity, blocking status, location, owner, lifecycle, and resolution evidence.

### Gate Decision

Stores gate type, reviewer, target OID, status, findings, evidence refs, and timestamp.

### Workspace Lease

Stores branch, worktree, build root, owner role, work-item revision, base OID, write scope, lease status, and expiration.

## Identity Rules

- IDs are stable and never reused.
- Revisions are monotonically increasing within their owning entity.
- Git OIDs are full commit OIDs at durable boundaries.
- A message sequence is monotonic within one SQLite database.
- Artifact references are immutable after publication.
- A decision never changes its target OID.
- Finding resolution creates new evidence; it does not erase the original finding.

## Status Ownership

| Status family | Owner |
|---|---|
| Goal and workstream | Goal controller |
| Module phase and iteration | Module controller |
| Work-item revision | PL-controlled work registry |
| Message delivery | SQLite queue |
| Task execution | Airflow plus project correlation record |
| Workspace lease | Workspace allocator |
| Finding lifecycle | Gate and review subsystem |
| Integration and release | Integration and release controllers |

## Persistence Projection

Target SQLite control tables:

```text
goals
plan_revisions
workstreams
module_loops
work_items
work_item_revisions
agent_turns
threads
messages
thread_snapshots
decisions
findings
workspace_leases
artifact_refs
integrations
releases
```

The current implementation contains `messages`, `outbox`, and `thread_snapshots`.

## Current Implementation Status

Specified. Message, outbox-event, and thread-snapshot entities exist in code. The broader control-state schema is not implemented.

## Consumed By

- [Goal Intake](../workflow/01-goal-intake.md)
- [Plan IR and Task DAG](../workflow/03-plan-ir-and-task-dag.md)
- [Module Development Loop](../workflow/11-module-development-loop.md)
- [Goal Supervisory Loop](../workflow/12-goal-supervisory-loop.md)
