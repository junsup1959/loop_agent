# Workflow Overview

## Purpose

Define the temporal control flow from an approved local goal to a completed, rejected, or safely blocked result.

Static system boundaries and component ownership are defined by:

- [System Context](../architecture/00-system-context.md);
- [Component Layers](../architecture/01-component-layers.md);
- [Domain and State Model](../architecture/03-domain-and-state-model.md).

## End-to-End Flow

```text
Goal Intake
  -> Discovery and Source Evidence
  -> Sequential Planning
  -> Validated Plan IR
  -> Role and Skill Binding
  -> TaskFlow DagRun
  -> Workspace Allocation
  -> Context Compilation
  -> Agent Task Execution
  -> Build and Verification
  -> Review and Approval
  -> Rework or Integration
  -> Module Result
  -> Goal Supervisory Decision
  -> Goal Completed or Blocked
```

## Entry Conditions

- The project architecture validates against local policy.
- Required role, skill, state, Git, and artifact components are available for the requested scope.
- The human project owner or approved upstream system supplied a goal.

## Workflow Rules

1. Complete goal intake before creating implementation work.
2. Gather source evidence before planning affected code paths.
3. Validate a Plan IR revision before scheduling tasks.
4. Bind one accountable role and an explicit skill packet to each work-item revision.
5. Allocate isolated mutable state before invoking a writer.
6. Compile context from durable systems of record.
7. Persist results and outgoing messages before acknowledging input.
8. Review and test exact commit OIDs.
9. Treat changed evidence as a new revision or plan, not an infrastructure retry.
10. Evaluate module and goal completion from persisted evidence.

## Terminal Outcomes

| Outcome | Meaning |
|---|---|
| `GOAL_COMPLETED` | Every completion criterion is backed by evidence on the integrated OID. |
| `GOAL_REJECTED` | An authorized role rejected the objective or an essential design decision. |
| `GOAL_BLOCKED` | Evidence, authority, policy, environment, or budget prevents safe continuation. |

## Current Workflow Coverage

Available:

- idempotent full control-plane initialization, Python dependency verification, Sequential Thinking npm installation, project-local MCP configuration, and non-mutating health verification;
- project-local role templates, random seat initialization, durable seat registry, and custom-agent compilation;
- pinned GPT-5.6 role profiles and seat-to-skill resolution;
- project-local skill validation and explicit selection;
- message enqueue, claim, lease, retry, snapshot, and outbox flows;
- Git OID and message context compilation;
- one module-iteration TaskFlow sequence;
- runner result and outgoing-message persistence;
- human-only message observation.

Not yet available:

- goal intake persistence;
- Plan IR validation and DAG compilation;
- workspace allocation;
- automated seat activation;
- deterministic gate evaluation;
- module and goal controllers;
- integration and release workflows.

## Next Document

Continue with [Goal Intake](01-goal-intake.md).
