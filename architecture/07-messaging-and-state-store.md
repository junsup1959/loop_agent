# Messaging and State Store

## Purpose

Define SQLite as the durable local communication and control-state boundary while keeping source code and large evidence in their appropriate stores.

## Data Ownership

SQLite owns:

- role messages;
- message delivery state;
- message deduplication;
- leases and attempts;
- transactional outbox events;
- role-specific thread snapshots;
- future goal, work, loop, decision, finding, workspace, and artifact-reference state.

SQLite does not own:

- source code;
- full Git diffs;
- large build or test logs;
- binary artifacts;
- human console output;
- the complete model context.

## Current Tables

### `messages`

Key columns:

```text
seq
id
thread_id
work_item_id
parent_message_id
from_role
to_role
type
priority
payload_json
status
available_at
claimed_by
lease_until
attempts
max_attempts
dedupe_key
last_error
created_at
processed_at
```

Allowed delivery states:

```text
PENDING
CLAIMED
RUNNING
ACKED
RETRY
DEAD_LETTER
```

### `outbox`

Stores a durable notification event for each newly committed message.

Allowed states:

```text
PENDING
PUBLISHED
RETRY
DEAD_LETTER
```

### `thread_snapshots`

Stores:

- thread and work-item IDs;
- target role;
- covered-through message sequence;
- compact projection payload;
- creation timestamp.

## Message Envelope

```json
{
  "id": "msg-1024",
  "thread_id": "thread-W42",
  "work_item_id": "W-42",
  "parent_message_id": "msg-1008",
  "from_role": "dev_1",
  "to_role": "ta",
  "type": "REVIEW_REQUEST",
  "priority": 50,
  "payload": {
    "repo_id": "product",
    "base_oid": "71ae234f9c...",
    "head_oid": "d920f31a82...",
    "context_profile": "architecture-review"
  },
  "status": "PENDING",
  "dedupe_key": "W-42:architecture:d920f31a82"
}
```

Payloads contain identifiers, OIDs, compact decision data, and artifact references. They do not contain authoritative code copies.

## Delivery Semantics

```text
at-least-once delivery
+ unique deduplication key
+ idempotent consumer
+ expiring lease
+ bounded attempts
```

## Transactional Outbox

```text
BEGIN IMMEDIATE
  -> INSERT message
  -> INSERT outbox event
COMMIT
```

Post-commit wake-up failure does not lose the outbox event.

## Dispatcher

The dispatcher:

- receives best-effort UDP wake signals on loopback;
- falls back to periodic polling;
- debounces wake bursts;
- drains bounded outbox batches;
- invokes a configured local handler;
- marks publication success or retry.

UDP is not a message store and does not carry the full durable payload.

## Snapshot Projection

Context reconstruction uses:

```text
latest snapshot for target role
+ messages after covered-through sequence
```

Snapshots never delete raw messages.

## Target Control Tables

The full architecture adds:

```text
goals
plan_revisions
workstreams
module_loops
work_items
work_item_revisions
agent_turns
threads
decisions
findings
workspace_leases
artifact_refs
integrations
releases
```

## Database Settings

Current queue connections enable:

- foreign keys;
- configurable busy timeout;
- WAL journal mode;
- normal synchronous mode.

Schema migration policy remains an open implementation decision.

## Current Implementation Status

Partial. Current message, outbox, and snapshot schemas plus delivery transitions, leases, retry, dead-letter, deduplication, and dispatcher exist. The broader control-state schema and role-level message authorization do not.

## Consumed By

- [Message Routing and Agent Lifecycle](../workflow/07-message-routing-and-agent-lifecycle.md)
- [Context Compilation](../workflow/08-context-compilation.md)
- [Failure and Recovery](../workflow/14-failure-and-recovery.md)
