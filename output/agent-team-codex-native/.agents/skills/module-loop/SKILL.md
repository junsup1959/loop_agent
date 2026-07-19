---
name: module-loop
description: Advance one bounded module through discovery, design, implementation, verification, review, rework, and integration evidence. Use when a development module needs a durable, dependency-aware execution loop.
---

# Module Loop

Operate one module at a time under its approved parent goal and Plan IR revision. Preserve the module contract; return control to the goal loop when that contract changes.

## Preconditions

Require the module objective, projected acceptance criteria, dependency status, authority boundary, write scope, required gates, budget, target repository, and durable module state. Do not activate implementation until the owner, workspace, and selected expertise skills are explicit.

## State Flow

```text
DISCOVERING -> DESIGNING -> READY_FOR_IMPLEMENTATION -> IMPLEMENTING
IMPLEMENTING -> VERIFYING -> REVIEWING
REVIEWING -> APPROVED -> INTEGRATING -> COMPLETED
REVIEWING -> CODE_REWORK -> IMPLEMENTING
REVIEWING -> DESIGN_REWORK -> DESIGNING
REVIEWING -> NEED_EVIDENCE -> DISCOVERING
REVIEWING -> CROSS_MODULE_IMPACT -> PARENT_REPLAN
```

## Iteration Procedure

1. Load durable module state and remaining budget.
2. Identify the current phase's smallest missing evidence.
3. Activate a bounded `research-loop` only when external or large local evidence is the smallest missing condition. Consume its final conclusion or a selected evidence artifact; do not attach the whole research corpus to the module.
4. Select one accountable role and the minimum eligible expertise skills.
5. Compile a role-specific context packet and validate its scope before activation.
6. Run exactly one TaskFlow module-iteration DagRun.
7. Persist result artifacts, structured SQLite messages, test evidence, and Git OIDs.
8. Evaluate required gate decisions and transition state, create a revision, request parent replan, or terminate.

## Minimum Context Rule

Inject only the current thread and work item, target role and seat, selected skill files, relevant snapshot and messages, verified base and head OIDs, selected changed paths, and phase-specific evidence. Do not inject other role traffic, unrelated modules, unselected skills, full Serena memories, or broad repository dumps.

Require the packet to remain within its declared profile budget. Record omissions and selection reasons so a reviewer can verify why every message, path, and skill was included. If needed evidence cannot fit, request a narrower follow-up context rather than expanding the packet implicitly.

## Review and Rework

Accept `APPROVED` only with the required review, quality, build, and integration evidence for the submitted OID. Route `CODE_REWORK`, `DESIGN_REWORK`, and `NEED_EVIDENCE` to a new work-item revision with an explicit delta. Route `CROSS_MODULE_IMPACT` to `PARENT_REPLAN`; do not modify another module's contract directly.

## Terminal Outcomes

Use only `COMPLETED`, `REJECTED`, `BLOCKED`, or `PARENT_REPLAN`. Repeated failure must change evidence or strategy. Budget exhaustion routes to `BLOCKED`.

This skill defines a bounded control loop. It does not bypass organizational approval, allocate unowned write scope, select a model, or grant tools or external access.
