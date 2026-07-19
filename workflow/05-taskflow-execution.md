# TaskFlow Execution

## Purpose

Execute one validated, immutable plan revision with deterministic dependencies and bounded infrastructure retries.

## Architecture Contract

- Plan IR, DAG families, and Airflow responsibility: [Planning and Orchestration](../architecture/06-planning-and-orchestration.md)
- Runner request and result interfaces: [Agent Runtime Interfaces](../architecture/10-agent-runtime-interfaces.md)
- Windows and POSIX process topology: [Runtime Deployment](../architecture/13-runtime-deployment.md)

## Entry Conditions

- The Plan IR revision and compiled DAG validate.
- Work-item ownership and skill binding are fixed.
- Required repositories, workspaces, SQLite state, and artifact roots are available.
- DagRun configuration names immutable input OIDs.

## Current Module Iteration

```text
load_runtime_conf
  -> compile_role_context
  -> execute_role_agent
  -> persist_result_and_messages
```

## Execution Workflow

1. Create a DagRun for one immutable plan or loop iteration.
2. Validate runtime configuration before any side effect.
3. Resolve required project paths and immutable OIDs.
4. Execute tasks in compiled dependency order.
5. Compile role context immediately before the bounded agent turn.
6. Invoke the runner under the configured timeout and retry policy.
7. Validate the structured runner result.
8. Persist the result artifact and outgoing messages.
9. Emit a durable iteration result for the owning controller.
10. Finish the DagRun without mutating its topology.

## Retry Boundary

A task retry keeps:

- the same Plan IR revision;
- the same work-item revision;
- the same role and skill binding;
- the same context input contract;
- the same base and head OIDs.

Changed evidence or intent creates a new workflow iteration, rework revision, or Plan IR revision.

## Failure Routes

| Condition | Route |
|---|---|
| Invalid DagRun configuration | Fail before side effects. |
| Context source missing | Emit `CONTEXT_SOURCE_MISSING`; do not invoke the agent. |
| Runner process transient failure | Retry with identical input. |
| Runner output invalid | Fail and retain stdout and stderr artifacts. |
| Agent requests context or rework | Persist the message and finish the iteration. |
| Plan change required | Finish the DagRun and create a new Plan IR revision. |

## Exit Conditions

- every scheduled task has a terminal state;
- result and message persistence is durable;
- the owning controller can determine the next workflow state;
- no active task depends on in-memory agent state.

## Implementation Status

Partial. The four-task module iteration is implemented. General DAG compilation, other DAG families, and controller-driven scheduling are not complete.

## Related Documents

- [Plan IR and Task DAG](03-plan-ir-and-task-dag.md)
- [Agent Task Execution](09-agent-task-execution.md)
- [Module Development Loop](11-module-development-loop.md)
