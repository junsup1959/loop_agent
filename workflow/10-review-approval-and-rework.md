# Review, Approval, and Rework

## Purpose

Route one immutable implementation revision through independent gates and create a new revision when evidence requires change.

## Architecture Contract

- Gate types, finding and decision schemas, OID pinning, and aggregation: [Gate and Evidence Model](../architecture/11-gate-and-evidence-model.md)
- Reviewer authority and separation of duty: [Team and Authority Model](../architecture/04-team-and-authority-model.md)
- Review packet structure: [Context and Evidence System](../architecture/08-context-and-evidence-system.md)

## State Flow

```text
SUBMITTED
  -> UNDER_REVIEW
     -> APPROVED
     -> CHANGES_REQUESTED
        -> REWORK
        -> RESUBMITTED
     -> NEED_MORE_CONTEXT
        -> CONTEXT_EXPANDED
        -> UNDER_REVIEW
     -> REJECTED
```

## Entry Conditions

- The submitted head OID resolves.
- Reviewer independence and authority validate.
- Required build and test prerequisites target the submitted OID.
- A detached review worktree can be created.

## Review Workflow

1. Verify the reviewer's role and independence.
2. Create a detached worktree at the submitted head OID.
3. Compile a role-specific review packet.
4. Verify prerequisite artifacts and their target OIDs.
5. Evaluate only the requested gate and affected boundary.
6. Persist findings and a structured decision.
7. Route approval to the next gate or route findings to rework.

## Context Versus Defect

- Use `NEED_MORE_CONTEXT` when evidence is insufficient.
- Use `CHANGES_REQUESTED` for an actionable defect or contract risk in the current OID.
- Use `REJECTED` when the current work-item scope cannot satisfy the approved contract.

## Rework Workflow

1. Create a new work-item revision.
2. Carry forward unresolved findings.
3. Rebind role and skills when required.
4. Compile delta context from the last reviewed OID.
5. implement and commit remediation.
6. map every addressed finding to evidence.
7. rerun affected build and test paths.
8. submit the new OID as `REWORK_SUBMITTED`.
9. compile a delta review packet.
10. rerun every affected gate.

## Approval Check

Before routing to integration:

1. load required gates from the Plan IR;
2. load latest decisions and open findings;
3. verify every decision target OID;
4. verify tested and integration candidate OIDs;
5. reject mismatches or missing gates;
6. emit integration eligibility.

## Failure Routes

| Condition | Route |
|---|---|
| Reviewer lacks authority or independence | Reassign reviewer. |
| Context incomplete | `NEED_MORE_CONTEXT` |
| Blocking finding | New rework revision |
| Architecture rejected | Return to design |
| Submitted branch advanced | Compile new OID context and rerun affected gates. |
| Gate evidence OID mismatch | Invalidate integration eligibility. |

## Exit Conditions

- a structured decision is durable;
- findings are traceable to evidence and an OID;
- the next gate, rework, design, context, or rejection route is explicit;
- approval never implies a different gate passed.

## Implementation Status

Specified. Queue and context foundations can carry review data. Decision persistence, finding lifecycle, independence checks, gate aggregation, and OID evaluation are not implemented.

## Related Documents

- [Context Compilation](08-context-compilation.md)
- [Module Development Loop](11-module-development-loop.md)
- [Integration and Release](13-integration-and-release.md)
