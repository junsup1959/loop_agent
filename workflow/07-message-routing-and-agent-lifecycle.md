# Message Routing and Agent Lifecycle

## Purpose

Deliver durable role messages and activate disposable agent processes without loading the complete queue into model context.

## Architecture Contract

- SQLite tables, envelope, delivery states, outbox, and snapshots: [Messaging and State Store](../architecture/07-messaging-and-state-store.md)
- Process and runner boundary: [Agent Runtime Interfaces](../architecture/10-agent-runtime-interfaces.md)
- Human observation boundary: [Observability Data Model](../architecture/15-observability-data-model.md)

## Routing Workflow

1. Validate sender, recipient, message type, payload, and deduplication key.
2. Commit the message and outbox event atomically.
3. Issue best-effort local wake-up after commit.
4. Drain the durable outbox in a bounded batch.
5. Resolve the target seat ID and locate ready messages for its internal role key.
6. Claim messages under an expiring lease.
7. Mark the input running before agent activation.
8. Persist agent result and outgoing messages.
9. Acknowledge success or schedule retry.
10. Move exhausted deliveries to dead-letter state.

## Agent Lifecycle

```text
IDLE
  -> message or task available
  -> claim lease
  -> load seat, role, pinned model profile, skills, context, and workspace
  -> execute bounded task
  -> persist result, evidence, and outgoing messages
  -> acknowledge input
  -> IDLE or terminate
```

## Batching

Recommended activation batch:

```text
(work_item_id, thread_id, to_seat_id)
```

Blocking decisions wake immediately. Low-priority progress and evidence updates may be merged into the next activation.

## Human-Only Observation

After commit:

```text
message
  -> shell echo or local log
  -> optional viewer and Git OID resolution
```

Observation output does not automatically create new messages, snapshots, state transitions, or model context.

## Failure Routes

| Condition | Route |
|---|---|
| Wake signal lost | Poll the durable outbox. |
| Consumer terminates | Let the lease expire and redeliver. |
| Duplicate send | Return the existing message by deduplication key. |
| Handler transient failure | Schedule bounded retry. |
| Attempts exhausted | Move to dead letter and emit operational evidence. |
| Seat, role, or type unauthorized | Reject before enqueue or activation. |

## Exit Conditions

- durable message state reflects the result;
- no message is considered delivered from wake-up alone;
- agent memory is reconstructable after process termination;
- failed deliveries remain observable.

## Implementation Status

Partial. Project-local seat resolution, queue delivery, snapshots, outbox, wake-up, polling, echo, and viewing exist. Queue envelopes still address role keys, and seat-level authorization plus automatic activation are not connected.

## Related Documents

- [Context Compilation](08-context-compilation.md)
- [Agent Task Execution](09-agent-task-execution.md)
- [Observability and Audit](15-observability-and-audit.md)
