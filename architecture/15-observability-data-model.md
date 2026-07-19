# Observability Data Model

## Purpose

Define correlation identifiers, event records, artifact metadata, human observation boundaries, and the audit chain required to reconstruct autonomous work.

## Correlation Identifiers

```text
goal_id
goal_revision
plan_id
workstream_id
module_loop_id
work_item_id
work_item_revision
iteration
role_id
skill_ids
thread_id
message_id
repo_id
base_oid
head_oid
workspace_id
artifact_ref
decision_id
finding_id
integration_id
release_id
dag_run_id
```

Each record carries only applicable identifiers, but the evidence chain must remain joinable.

## Event Record

```json
{
  "event_id": "EVT-001",
  "event_type": "REVISION_SUBMITTED",
  "occurred_at": "timestamp",
  "producer": "agent-runner",
  "role_id": "dev_1",
  "goal_id": "G-001",
  "plan_id": "PLAN-G001-R1",
  "work_item_id": "W-42",
  "work_item_revision": 3,
  "repo_id": "product",
  "head_oid": "d920f31a82...",
  "artifact_refs": []
}
```

## Suggested Event Types

```text
GOAL_STATE_CHANGED
PLAN_VALIDATED
WORK_ITEM_ASSIGNED
SKILLS_BOUND
WORKSPACE_ALLOCATED
CONTEXT_COMPILED
AGENT_STARTED
AGENT_FINISHED
MESSAGE_ENQUEUED
MESSAGE_ACKED
REVISION_SUBMITTED
GATE_DECIDED
FINDING_OPENED
FINDING_RESOLVED
INTEGRATION_CREATED
RELEASE_VALIDATED
LOOP_BLOCKED
```

## Artifact Metadata

```json
{
  "artifact_ref": "artifact://tests/W-42/r3/unit.json",
  "content_type": "application/json",
  "producer": "qa-runner",
  "role_id": "qa_sdet",
  "work_item_id": "W-42",
  "work_item_revision": 3,
  "target_oid": "d920f31a82...",
  "local_path": ".agent-team/artifacts/tests/W-42/r3/unit.json",
  "sha256": "hex-digest",
  "created_at": "timestamp",
  "retention_class": "goal-evidence"
}
```

## Audit Chain

```text
Goal Revision
  -> Plan Revision
  -> Work-Item Revision
  -> Role and Skill Binding
  -> Context Artifact
  -> Workspace Lease
  -> Base and Head OIDs
  -> Build and Test Artifacts
  -> Findings and Gate Decisions
  -> Integration OID
  -> Release Evidence
```

## Human Observation Boundary

Human observation may read:

- committed messages;
- delivery status;
- Git OIDs;
- changed paths and bounded diffs;
- local logs and artifacts;
- controller and gate state.

Human observation output does not automatically:

- create a message;
- update a snapshot;
- change controller state;
- enter a model prompt;
- approve a gate.

## Current Observation Components

- shell message echo;
- optional local message log;
- dispatcher stdout;
- message viewer with filters and watch mode;
- optional Git diff reconstruction;
- JSON context and result artifacts.

## Retention Classes

Recommended classes:

| Class | Example |
|---|---|
| Active control | Current goal, work, message, and lease records |
| Goal evidence | Approvals, tests, integration, release artifacts |
| Diagnostic | Failed runner output and transient logs |
| Human observation | Console or local message logs |
| Archive | Rejected or superseded refs and decisions retained by policy |

Retention deletion must check open references before removing data.

## Current Implementation Status

Partial. Message sequences, IDs, OIDs, context and result artifacts, echo, logs, viewing, and dispatcher events exist. Unified event storage, artifact hashes, retention classes, controller views, and complete audit joins are not implemented.

## Consumed By

- [Observability and Audit](../workflow/15-observability-and-audit.md)
- [Review, Approval, and Rework](../workflow/10-review-approval-and-rework.md)
- [Integration and Release](../workflow/13-integration-and-release.md)
