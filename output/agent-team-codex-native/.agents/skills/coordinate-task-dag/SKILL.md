---
name: coordinate-task-dag
description: Decompose software work into an executable task DAG with bounded ownership, explicit inputs and outputs, review gates, retries, and integration points. Use when multiple agents or stages must cooperate without free-form conversation.
---

# Coordinate Task DAG

Produce a deterministic execution graph that a workflow runtime can schedule and audit.

## Procedure

1. Use structured reasoning to identify the objective, alternatives, and dependency edges.
2. Split work into nodes with one accountable organizational role per write-critical scope.
3. Define each node's immutable inputs, expected artifacts, messages, and completion evidence.
4. Mark fan-out, join, review, approval, retry, and compensation edges.
5. Define wait conditions and the events that wake an agent.
6. Verify the graph has no hidden dependency, duplicate ownership, or unbounded loop.

## Node Contract

Each node must declare:

- `task_id`, objective, and assigned role;
- required Git OIDs and context artifact references;
- selected expertise skill IDs;
- predecessor task IDs;
- output artifact and message types;
- validation and approval gate;
- retry budget and terminal failure behavior.

## Quality Rules

- Keep urgent critical-path work local to its responsible role.
- Allow parallel work only for disjoint or explicitly coordinated scopes.
- Use structured messages and Git references instead of long conversational payloads.
- Route approval to the organizational authority, not to a skill.
- Make every loop bounded by a retry count, deadline, or escalation condition.

## Authority Boundary

This skill designs execution structure only. It does not spawn agents, approve work, select models, grant permissions, or execute the DAG.
