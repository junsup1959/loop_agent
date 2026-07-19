# Failure and Recovery

## Purpose

Classify failure and choose retry, context expansion, rework, replan, escalation, or blocked termination.

## Architecture Contract

- Message leases, retry, dead-letter, and outbox recovery: [Messaging and State Store](../architecture/07-messaging-and-state-store.md)
- Controller budgets and durable blocked state: [Loop Control Model](../architecture/12-loop-control-model.md)
- Runner retry identity: [Agent Runtime Interfaces](../architecture/10-agent-runtime-interfaces.md)

## Failure Classes

| Class | Control action |
|---|---|
| Infrastructure retry | Retry identical task input. |
| Delivery retry | Expire or release lease and redeliver idempotently. |
| Context expansion | Add evidence and repeat the same revision. |
| Code rework | Create a new work-item revision and OID. |
| Design rework | Return to design and create a new approved revision. |
| Replan | Create a new Plan IR revision and DagRun. |
| Escalation | Route a structured request to a higher authority. |
| Blocked | Persist terminal blocked evidence. |

## Classification Workflow

1. Record the failed operation and immutable input identifiers.
2. determine whether the failure is transient or evidence-bearing.
3. check duplicate side-effect risk.
4. identify whether context, code, design, scope, dependency, role, or skill must change.
5. select one primary control action.
6. persist classification, owner, attempts, next action, and deadline.
7. enforce the class-specific budget.

## Retry Invariant

A retry keeps Plan IR revision, work-item revision, role, skill binding, context input contract, workspace input state, OIDs, and output contract unchanged.

Any change routes to rework or replan.

## Repeated Failure Rule

After retry exhaustion, the next attempt changes at least one of:

- context;
- hypothesis;
- design;
- role;
- skill;
- scope;
- validation method.

## Recovery Matrix

| Failure | Recovery |
|---|---|
| Model, MCP, or runner transient error | Bounded TaskFlow retry |
| Agent process termination | Lease expiration and redelivery |
| Wake loss | Outbox polling |
| Missing context | Context expansion |
| Review finding | Rework revision |
| Test failure | Finding routed to implementation |
| Invalid Plan IR | Replan |
| Missing Git OID | `CONTEXT_SOURCE_MISSING` |
| Approval OID changed | Invalidate and rerun gates |
| Integration conflict | Integration work item |
| Contract conflict | TA decision and replan |
| Rollback failure | Release blocked |
| Budget exhausted | Module or goal blocked |

## Exit Conditions

- one failure class is persisted;
- retry identity or revision change is explicit;
- the next owner and action are known;
- blocked results retain complete evidence.

## Implementation Status

Partial. Queue, outbox, lease, and runner retry primitives exist. Workflow classification, rework, replanning, escalation, and durable blocked records are not complete.

## Related Documents

- [TaskFlow Execution](05-taskflow-execution.md)
- [Module Development Loop](11-module-development-loop.md)
- [Goal Supervisory Loop](12-goal-supervisory-loop.md)
