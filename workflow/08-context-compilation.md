# Context Compilation

## Purpose

Assemble the smallest role-specific evidence packet required for the next action.

## Architecture Contract

- Context sources, packet structure, role lenses, semantic adapters, and non-compressible data: [Context and Evidence System](../architecture/08-context-and-evidence-system.md)
- Project-local skill packet: [Expertise Skill System](../architecture/05-expertise-skill-system.md)
- Artifact and audit metadata: [Observability Data Model](../architecture/15-observability-data-model.md)

## Entry Conditions

- The target role, work item, revision, and required action are known.
- The selected skill packet validates.
- The repository registry resolves the repository.
- Required base and head OIDs identify commits.
- The requested context profile exists.

## Compilation Workflow

1. Resolve the repository.
2. Verify base and head OIDs.
3. Load the latest target-role snapshot.
4. Load only messages after the snapshot sequence for the same work item and target role.
5. Recompute changed paths from Git.
6. Collect bounded Git and commit evidence.
7. Select only the Serena memory references required by the target role and action.
8. Add target-OID-pinned Serena semantic evidence when required and available.
9. Resolve required artifacts.
10. Resolve selected project-local skills.
11. Apply the target-role lens and fail-closed context budget.
12. Record omitted or truncated context.
13. Persist the immutable context artifact.

## Snapshot Use

Each activation receives:

```text
latest role snapshot
+ messages after the snapshot
+ latest Git delta
+ evidence required by the current action
```

Create a new snapshot only after the role projection is complete and traceable through a message sequence.

## Review Context

For a review:

1. state the exact decision requested;
2. include the relevant acceptance and contract evidence;
3. pin actual Git evidence to base and head OIDs;
4. include prior findings and resolution mapping;
5. include build and test artifacts;
6. disclose omitted context.

Insufficient evidence routes to `NEED_MORE_CONTEXT`.

## Budgeting Workflow

1. Start with the latest snapshot and delta.
2. Prefer changed paths and direct symbol neighborhoods.
3. expand only the evidence level required by the role and explicit action.
4. keep full evidence in local storage.
5. reference selected Serena memories instead of preloading every memory or unrelated source.
6. disclose every enforced limit.

## Serena Knowledge Selection

Every role may use the project-shared Serena endpoint for a targeted semantic question. The Context Compiler must include only the memory references and semantic-evidence artifacts needed by the current action. It must not treat a live Serena server, whole memory collection, or prior agent conversation as implicit context.

Shared Serena memory remains slow-changing project knowledge. Only the PL may publish or refresh it. When another role identifies a durable fact, it sends a concise evidence-backed proposal through SQLite; the proposal is not injected as shared memory until the PL acknowledges it.

## Failure Routes

| Condition | Route |
|---|---|
| Repository or OID missing | `CONTEXT_SOURCE_MISSING` |
| Skill binding invalid | Return to role and skill binding. |
| Diff exceeds limit | Mark truncation and offer explicit expansion. |
| Required artifact missing | `NEED_MORE_CONTEXT` or failed prerequisite. |
| Semantic analyzer unavailable | Record the limitation and use an approved fallback. |
| Serena memory not selected or unavailable | Continue without it when the action is still evidenced; otherwise emit `NEED_MORE_CONTEXT`. |

## Exit Conditions

- context belongs to one target role and action;
- Git evidence is immutable;
- selected skills are project-local;
- omitted context is explicit;
- the artifact is reusable after process restart.

## Implementation Status

Partial. Work-item-scoped Git and message compilation, role/profile budgets, selected path enforcement, JSON context artifacts, and selected-skill materialization exist. Artifact resolution and semantic adapters are not connected.

## Related Documents

- [Discovery and Source Evidence](02-discovery-and-source-evidence.md)
- [Agent Task Execution](09-agent-task-execution.md)
- [Review, Approval, and Rework](10-review-approval-and-rework.md)
