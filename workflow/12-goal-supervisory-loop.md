# Goal Supervisory Loop

## Purpose

Coordinate workstreams and module loops until goal completion evidence is sufficient or safe continuation is impossible.

## Architecture Contract

- Goal and module controller ownership, phases, budgets, and parent-child boundaries: [Loop Control Model](../architecture/12-loop-control-model.md)
- Goal, plan, workstream, integration, and release entities: [Domain and State Model](../architecture/03-domain-and-state-model.md)

## State Flow

```text
DEFINE_GOAL
  -> DECOMPOSE
  -> RUN_WORKSTREAMS
  -> INTEGRATE
  -> SYSTEM_VERIFY
     -> GOAL_COMPLETED
     -> MODULE_DEFECT
        -> affected module loop
     -> CONTRACT_CONFLICT
        -> TA decision and replan
     -> PLAN_INVALID
        -> Sequential Thinking replan
     -> EVIDENCE_MISSING
        -> verification work item
     -> POLICY_BLOCKED
        -> GOAL_BLOCKED
```

## Entry Conditions

- A validated goal and authority boundary exist.
- Durable state, local repositories, and artifact storage are available.
- The planning provider can create or revise Plan IR.

## Supervisory Cycle

1. Load goal, plan, workstream, module, finding, gate, and budget state.
2. compare acceptance criteria with available evidence.
3. create or revise the Plan IR when required.
4. activate ready workstreams.
5. allow independent module loops to progress.
6. consume module completion, blocked, contract-change, and replan events.
7. update dependency and integration readiness.
8. create integration work when required gates pass.
9. run system verification.
10. complete, route defects, replan, escalate, or block.

## Contract Change Propagation

1. Pause dependent integration.
2. Route the proposal to TA and PL.
3. identify affected modules.
4. approve, reject, or revise the contract.
5. create a new Plan IR revision when dependencies or scopes change.
6. invalidate affected gates.
7. resume only with updated module contracts.

## Completion Evaluation

The goal completes only when:

- every acceptance criterion has evidence;
- required modules are completed;
- integration and system verification pass;
- required gates target the final integration OID;
- no blocking finding remains;
- residual risks are within approved policy;
- no contract-change event remains unresolved.

## Failure Routes

| Condition | Route |
|---|---|
| Module defect | Reactivate owning module loop. |
| Contract conflict | TA decision and Plan IR revision |
| Plan invalid | Sequential Thinking replan |
| Evidence missing | Create verification work |
| Authority or policy exceeded | `GOAL_BLOCKED` |
| Goal budget exhausted | `GOAL_BLOCKED` |

## Implementation Status

Specified. Goal persistence, supervisory scheduling, cross-module propagation, completion evaluation, and goal budgets are not implemented.

## Related Documents

- [Goal Intake](01-goal-intake.md)
- [Module Development Loop](11-module-development-loop.md)
- [Integration and Release](13-integration-and-release.md)
