# Agent Runtime Interfaces

## Purpose

Define the stateless role-agent process boundary, instruction assembly order, runner request and response schemas, and durable result handoff.

## Runtime Components

```text
Agent Activation Controller
  -> Seat Registry and Role Contract Resolver
  -> Skill Resolver
  -> Context Artifact Loader
  -> Workspace Lease Resolver
  -> Instruction Assembler
  -> Model Runner
  -> Result Validator
  -> Artifact and Message Persister
```

## Process Model

- A seat ID is durable and human-visible.
- An ASCII role key is the internal authority and skill-policy identity.
- A model process is disposable.
- One activation handles one bounded task.
- The process receives no implicit team memory, whole Serena-memory set, or historical task transcript.
- Every required input is reconstructed from project-local durable state.

## Instruction Assembly Order

```text
1. project policy and safety boundary
2. organizational role and authority
3. goal and work-item contract
4. selected project-local skill contents
5. selected Serena memory references and semantic evidence within the role-specific context packet
6. workspace and write-scope contract
7. required output schema
8. timeout, tool, and environment limits
```

Lower-priority instructions may narrow execution but may not expand authority.

## Runner Request

```json
{
  "goal_id": "G-001",
  "plan_id": "PLAN-G001-R1",
  "work_item_id": "W-42",
  "work_item_revision": 3,
  "thread_id": "thread-W42",
  "iteration": 3,
  "actor_seat_id": "DEV_<generated-name>",
  "actor_role_key": "dev_1",
  "agent_file": ".codex/agents/DEV_<generated-name>.toml",
  "context_profile": "implementation",
  "context_path": ".agent-team/artifacts/contexts/W-42/iteration-3.json",
  "skill_packet": {
    "explicit_injection": true,
    "skills": []
  },
  "workspace": {
    "workspace_id": "WS-W42-DEV1-R3",
    "path": ".agent-team/worktrees/W-42-dev-1-r3",
    "branch": "work/W-42/dev_1/3",
    "base_oid": "71ae234f9c...",
    "write_scope": []
  },
  "budget": {
    "timeout_seconds": 1800
  }
}
```

The current TaskFlow implementation sends goal, work-item, thread, iteration, role key, context profile, and a bounded context path. When a seat and selected skills are supplied, it also validates the seat-role binding, passes the compiled agent file, and materializes only the selected skill contents for the runner request.

Every role may use the shared Serena loopback endpoint for targeted semantic exploration after activation. That tool access does not grant memory publication or any higher organizational authority. Only the PL may publish or refresh shared Serena project memory; other roles route concise evidence-backed proposals through SQLite.

## Project-Local Custom Agents

Canonical sources under `agents/` define:

- six organizational role templates;
- eight logical slot specifications;
- one generated durable seat registry;
- six pinned model and sandbox profiles.

`scripts/project_agents.py sync` compiles each seat into one self-contained `.codex/agents/<seat_id>.toml` file. Every runtime file contains the required Codex custom-agent fields plus:

- a pinned `gpt-5.6-sol` or `gpt-5.6-terra` model;
- reasoning effort;
- sandbox mode;
- resolved seat identity, role authority, lifecycle boundary, and message contract.

## Model Runner Interface

The executable command is currently configured as a JSON string array through `AGENT_TEAM_RUNNER_COMMAND_JSON`.

The runner:

- reads one JSON request from standard input;
- writes exactly one JSON object to standard output;
- writes diagnostics to standard error;
- returns a nonzero exit code on process failure;
- respects a configured timeout.

## Runner Result

```json
{
  "status": "SUBMITTED",
  "head_oid": "d920f31a82...",
  "artifact_refs": [
    "artifact://tests/W-42/r3/unit.json"
  ],
  "outgoing_messages": [
    {
      "to_role": "ta",
      "type": "REVIEW_REQUEST",
      "priority": 50,
      "payload": {
        "review_type": "architecture"
      }
    }
  ]
}
```

## Allowed Terminal Results

```text
SUBMITTED
NO_CHANGE
NEED_MORE_CONTEXT
BLOCKED
REJECTED
```

The status vocabulary may be extended only through a versioned interface change.

## Result Persistence

The persistence adapter:

- validates the result object;
- validates outgoing message fields;
- adds goal, iteration, repository, and OID defaults when absent;
- creates deduplication keys;
- enqueues messages;
- writes the full result to a local artifact;
- returns compact artifact and message identifiers.

## Interface Invariants

- Agent stdout is one JSON object, not conversational text.
- A submitted code result includes a resolvable head OID.
- Large evidence is referenced, not embedded.
- The runner never approves its own result by implication.
- A timeout does not erase context or workspace state.
- Durable persistence completes before the input message is acknowledged.

## Current Implementation Status

Partial. Project-local custom-agent generation, seat and role resolution, pinned model profiles, subprocess execution, bounded context and selected-skill injection, JSON I/O, timeout, result validation, artifact persistence, and message persistence exist. The activation controller, workspace binding, write-scope enforcement, and complete model/tool policy adapter do not.

## Consumed By

- [Agent Task Execution](../workflow/09-agent-task-execution.md)
- [TaskFlow Execution](../workflow/05-taskflow-execution.md)
- [Failure and Recovery](../workflow/14-failure-and-recovery.md)
