# Observability and Audit

## Purpose

Operate and inspect the local agent team while preserving a reconstructable evidence chain and keeping human observation outside automatic model context.

## Architecture Contract

- Correlation identifiers, events, artifacts, audit chain, and retention: [Observability Data Model](../architecture/15-observability-data-model.md)
- Human and machine trust boundaries: [Security and Authority Boundaries](../architecture/14-security-and-authority-boundaries.md)

## Audit Questions

The operating workflow must be able to answer:

- Which role performed the task?
- Which skills were injected?
- Which context artifact and snapshot were used?
- Which repository, workspace, and OIDs were involved?
- Which tests ran against which OID?
- Which reviewer made each decision?
- Why did a gate fail?
- Which revision resolved a finding?
- Which evidence justified integration or release?
- Why did a controller stop as blocked?

## Observation Workflow

1. Select the goal, work item, role, thread, message, OID, or DagRun of interest.
2. read durable state without mutation.
3. resolve artifact and Git references.
4. disclose truncated evidence.
5. print or render human-readable output.
6. keep observation output outside automatic queue, snapshot, controller, and prompt inputs.

## Operational Views

Minimum views:

1. goal and workstream state;
2. module phase and budget;
3. active work items and workspace leases;
4. messages by role and delivery state;
5. open findings and required actions;
6. gate results by target OID;
7. integration and release evidence;
8. dead-letter and blocked records.

## Current Commands

View messages and optional Git changes:

```powershell
python .\scripts\agent_team_message_viewer.py `
  --db .\.agent-team\state\agent-team.db `
  --registry .\.agent-team\repositories.json `
  --role ta `
  --show-diff
```

Drain the durable outbox:

```powershell
python .\scripts\agent_team_dispatcher.py `
  --db .\.agent-team\state\agent-team.db `
  --once
```

Validate project-local skills:

```powershell
python .\scripts\project_skills.py validate
```

## Failure Routes

| Condition | Route |
|---|---|
| Evidence reference missing | Report incomplete audit chain. |
| Diff truncated | Request explicit expansion. |
| Human output contains sensitive data | Stop, redact, and record exposure handling. |
| State and artifact OIDs disagree | Mark evidence invalid. |
| Referenced artifact is pending cleanup | Retain until references close. |

## Exit Conditions

- requested evidence is displayed or absence is explicit;
- observation made no control-state change;
- sensitive values are protected;
- audit joins remain traceable.

## Implementation Status

Partial. Echo, local logs, message filters, watch mode, Git viewing, context and result artifacts exist. Unified events, controller views, artifact hashes, and retention automation do not.

## Related Documents

- [Message Routing and Agent Lifecycle](07-message-routing-and-agent-lifecycle.md)
- [Failure and Recovery](14-failure-and-recovery.md)
- [Architecture Index](../architecture/INDEX.md)
