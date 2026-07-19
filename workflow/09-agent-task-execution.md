# Agent Task Execution

## Purpose

Run one bounded organizational role turn and durably persist its structured result.

## Architecture Contract

- Agent activation, instruction assembly, request, response, and persistence interfaces: [Agent Runtime Interfaces](../architecture/10-agent-runtime-interfaces.md)
- Workspace and OID authority: [Git and Workspace System](../architecture/09-git-and-workspace-system.md)
- Tool, path, privilege, and authority boundaries: [Security and Authority Boundaries](../architecture/14-security-and-authority-boundaries.md)

## Entry Conditions

- The task or message lease is active.
- Role and skill binding validates.
- The context artifact exists.
- A writer has an isolated workspace and write scope.
- The output schema and execution budget are known.

## Execution Workflow

1. Mark the claimed input running.
2. Resolve and validate role, skill, context, workspace, and budget contracts.
3. Assemble instructions in architecture-defined precedence order.
4. Start the runner.
5. Permit only task-authorized reads, writes, tools, and external actions.
6. Require proportionate validation.
7. Require a commit for submitted code changes.
8. Parse and validate one result object.
9. Verify the submitted OID and write scope when applicable.
10. Persist the result artifact and outgoing messages.
11. Acknowledge input after durable persistence.

When the task needs semantic exploration, the runner uses the one project-shared Serena loopback endpoint. It reads only the memory references selected in the context packet and does not start an additional Serena process. All roles may explore source semantically, but only the PL may publish or refresh shared Serena project memory; other roles persist evidence-backed proposals through SQLite.

## Safe Completion

The agent turn returns one supported terminal result:

```text
SUBMITTED
NO_CHANGE
NEED_MORE_CONTEXT
BLOCKED
REJECTED
```

`BLOCKED` includes the blocking condition, exhausted alternatives, required change, owner, and evidence.

## Failure Routes

| Condition | Route |
|---|---|
| Runner transient failure | Retry identical input within budget. |
| Runner output invalid | Fail and preserve diagnostics. |
| Write-scope violation | Stop and emit `POLICY_BLOCKED`. |
| Context insufficient | Emit `CONTEXT_REQUIRED`. |
| Architecture decision required | Route to TA. |
| Review or test rework | Create a new work-item revision. |

## Exit Conditions

- runner result validates;
- code submissions resolve to a full head OID;
- artifacts and outgoing messages are durable;
- the input lease is acknowledged or recoverable;
- the next controller action is unambiguous.

## Implementation Status

Partial. Subprocess runner execution, JSON validation, timeout, result artifacts, and message persistence exist. Role instructions, skill injection, workspace binding, and policy enforcement are not connected.

## Related Documents

- [TaskFlow Execution](05-taskflow-execution.md)
- [Review, Approval, and Rework](10-review-approval-and-rework.md)
- [Failure and Recovery](14-failure-and-recovery.md)
