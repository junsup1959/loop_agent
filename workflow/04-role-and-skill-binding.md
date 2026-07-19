# Role and Skill Binding

## Purpose

Bind one accountable organizational role and the minimum task-specific expertise to a work-item revision.

## Architecture Contract

- Fixed roles and approval authority: [Team and Authority Model](../architecture/04-team-and-authority-model.md)
- Project-local skill packages, catalog, limits, and resolver: [Expertise Skill System](../architecture/05-expertise-skill-system.md)
- Work-item and revision ownership: [Domain and State Model](../architecture/03-domain-and-state-model.md)

## Entry Conditions

- The Plan IR work item is validated.
- The objective, read and write scopes, expected output, gates, and budget are known.
- Required skill packages exist in the project catalog.

## Selection Workflow

1. Read the work-item objective, source evidence, output contract, and required gates.
2. Select one accountable logical slot and resolve its durable seat ID.
3. Select the smallest workflow skill set needed for the current phase.
4. Select at most two technology skills that match the actual implementation boundary.
5. Validate eligibility and total selection limits.
6. Persist the seat ID, internal role key, and selected skill IDs on the work-item revision.
7. Resolve project-local runtime paths.
8. pass the binding to context compilation and agent activation.

Example:

```powershell
$developerSeat = (
  python .\scripts\project_agents.py list |
    ConvertFrom-Json |
    Where-Object role_key -eq "dev_1"
).seat_id

python .\scripts\project_agents.py resolve `
  --seat $developerSeat `
  --skill map-codebase `
  --skill engineer-dotnet-desktop `
  --skill engineer-local-data `
  --skill engineer-test-coverage
```

## Context Ordering

The runner receives:

1. seat identity, organizational role, and authority boundary;
2. goal and work-item contract;
3. selected project-local skill contents;
4. role-specific context snapshot;
5. Git and artifact evidence;
6. required output schema;
7. runtime safety and tool policy.

## Rebinding

Create a new binding before the next agent turn when:

- the workflow phase changes;
- new evidence changes the affected technology;
- review identifies missing expertise;
- repeated failure requires another diagnostic method;
- a contract change alters the platform or scope.

Do not mutate the binding of an active agent turn.

## Failure Routes

| Condition | Route |
|---|---|
| Unknown skill | Reject the binding before context compilation. |
| Role is ineligible | Return to PL for reassignment or skill replacement. |
| Skill count exceeds budget | Remove redundant expertise or split the work item. |
| Required expertise is absent | Block execution until a project-local skill exists. |
| Skill conflicts with authority | Preserve the authority boundary and route to PL. |

## Exit Conditions

- one accountable seat ID and role key are persisted;
- selected skills validate;
- the binding belongs to one work-item revision;
- no approval conflict or self-approval route exists;
- the binding is ready for context compilation.

## Implementation Status

Partial. Seat initialization, seat-to-role mapping, profile-pinned model selection, project-local skill validation, and explicit binding resolution exist. Assignment persistence and automatic runner injection are not connected.

## Related Documents

- [Plan IR and Task DAG](03-plan-ir-and-task-dag.md)
- [Context Compilation](08-context-compilation.md)
- [Agent Task Execution](09-agent-task-execution.md)
