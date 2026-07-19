# Module Development Loop

## Purpose

Advance one module or bounded behavior through discovery, design, implementation, verification, review, and integration.

## Architecture Contract

- Durable controller hierarchy, module phases, budgets, and iteration mapping: [Loop Control Model](../architecture/12-loop-control-model.md)
- Module, work-item, revision, and evidence entities: [Domain and State Model](../architecture/03-domain-and-state-model.md)

## State Flow

```text
DISCOVERING
  -> DESIGNING
  -> READY_FOR_IMPLEMENTATION
  -> IMPLEMENTING
  -> VERIFYING
  -> REVIEWING
     -> APPROVED
        -> INTEGRATING
        -> COMPLETED
     -> CODE_REWORK
        -> IMPLEMENTING
     -> DESIGN_REWORK
        -> DESIGNING
     -> NEED_EVIDENCE
        -> DISCOVERING
     -> CROSS_MODULE_IMPACT
        -> PARENT_REPLAN
```

## Entry Conditions

- The parent goal supplies the module objective, projected acceptance criteria, dependencies, authority boundary, required gates, and budget.
- Required predecessor modules are satisfied.
- The durable module-loop record exists.

## Iteration Workflow

1. Load durable module state and remaining budget.
2. identify the current phase's missing evidence.
3. classify the state gap.
4. select the next role and skill binding.
5. compile role context.
6. run one module-iteration DagRun.
7. persist results, artifacts, messages, and OIDs.
8. evaluate phase evidence and required gate decisions.
9. transition state, create rework, request parent replan, or terminate.

## Phase Exit Requirements

| Phase | Required evidence |
|---|---|
| Discovery | Bounded source evidence and unknowns |
| Design | Approved design or architecture decision |
| Ready for implementation | Valid role, skills, scopes, contracts, and workspace plan |
| Implementation | Submitted commit OID |
| Verification | Build and test artifacts tied to the submitted OID |
| Review | Required gate decisions and findings |
| Integration | Integrated OID and system evidence |
| Completion | Satisfied projected criteria and residual-risk record |

## Loop Rules

- One iteration maps to one DagRun.
- A state transition schedules the next iteration.
- Rework creates a work-item revision.
- Replan creates a Plan IR revision.
- Repeated failure must change evidence or strategy.
- Budget exhaustion routes to blocked.

## Parent Replan

Return `PARENT_REPLAN` when a public contract, dependency, write scope, authority boundary, or affected-module set changes.

## Terminal Outcomes

```text
COMPLETED
REJECTED
BLOCKED
PARENT_REPLAN
```

## Implementation Status

Partial. One module-iteration DAG exists. Durable module state, transition evaluation, gate routing, budgets, and next-iteration scheduling are not implemented.

## Related Documents

- [TaskFlow Execution](05-taskflow-execution.md)
- [Review, Approval, and Rework](10-review-approval-and-rework.md)
- [Goal Supervisory Loop](12-goal-supervisory-loop.md)
