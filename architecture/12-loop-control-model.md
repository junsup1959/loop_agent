# Loop Control Model

## Purpose

Define the durable controller hierarchy that wraps bounded agent turns into module development and goal supervision without using unbounded in-process loops.

## Controller Hierarchy

```text
Goal Controller
  -> Workstream Coordination
  -> Module Controller
  -> Work-Item Revision
  -> TaskFlow DagRun
  -> Agent Turn
```

Higher controllers coordinate results. They do not perform lower-level implementation directly.

## Module Controller

Owns:

- module objective and projected acceptance criteria;
- current phase;
- iteration number;
- active work item and revision;
- required role and skill binding;
- required gates;
- unresolved findings;
- module budget;
- parent-replan flag;
- terminal result.

Module phases:

```text
DISCOVERING
DESIGNING
READY_FOR_IMPLEMENTATION
IMPLEMENTING
VERIFYING
REVIEWING
INTEGRATING
COMPLETED
REJECTED
BLOCKED
PARENT_REPLAN
```

## Goal Controller

Owns:

- goal revision;
- active Plan IR revision;
- workstreams and module dependencies;
- goal-level budget;
- contract-change propagation;
- integration readiness;
- system verification;
- terminal goal result.

Goal phases:

```text
DEFINE_GOAL
DECOMPOSE
RUN_WORKSTREAMS
INTEGRATE
SYSTEM_VERIFY
GOAL_COMPLETED
GOAL_REJECTED
GOAL_BLOCKED
```

## Iteration Mapping

```text
durable controller state -> SQLite
one bounded iteration -> one DagRun
agent work -> one task or bounded task group
next state event -> new DagRun
material plan change -> new Plan IR revision
```

## Controller Decision Input

Controllers consume:

- current durable state;
- plan and work-item revisions;
- messages and required actions;
- submitted Git OIDs;
- build and test artifacts;
- gate decisions and findings;
- budget counters;
- contract-change and affected-module evidence.

## Controller Decision Output

```json
{
  "controller_id": "loop-runtime-01",
  "input_state": "REVIEWING",
  "output_state": "IMPLEMENTING",
  "reason": "Blocking finding F-W42-01 requires code rework.",
  "next_role": "dev_1",
  "next_skill_ids": [
    "engineer-cpp-systems",
    "engineer-test-coverage"
  ],
  "new_revision_required": true,
  "new_plan_required": false,
  "budget_remaining": {
    "iterations": 2
  }
}
```

## Budget Model

Controllers track:

- maximum iterations;
- repeated failure count by classification;
- Plan IR revision count;
- task runtime;
- model or token allocation when configured;
- context size;
- artifact storage;
- deadline or retention boundaries.

Budget exhaustion creates a blocked outcome, not implicit success.

## Repetition Rule

After repeated failure, the next iteration must change at least one:

- context;
- hypothesis;
- design;
- role;
- selected skill;
- scope;
- verification method.

Unchanged prompt repetition is not a control strategy.

## Current Implementation Status

Specified. The current TaskFlow DAG represents one module iteration, but durable controller schemas, transition evaluators, scheduling triggers, budgets, and parent-child contracts are not implemented.

## Consumed By

- [Module Development Loop](../workflow/11-module-development-loop.md)
- [Goal Supervisory Loop](../workflow/12-goal-supervisory-loop.md)
- [Failure and Recovery](../workflow/14-failure-and-recovery.md)
