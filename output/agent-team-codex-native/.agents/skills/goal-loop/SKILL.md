---
name: goal-loop
description: Supervise one bounded software-development goal across planning, module loops, approval gates, integration, and completion evidence. Use when initiating, resuming, replanning, or closing a multi-module autonomous development goal.
---

# Goal Loop

Operate the goal-level controller. Coordinate work; do not perform an individual module implementation in place of its owning module loop.

## Preconditions

Require a bounded objective, authority boundary, acceptance criteria, budget, local repository identity, and durable goal state. If any is missing, emit `NEED_MORE_CONTEXT` or `GOAL_BLOCKED`; do not infer the missing contract.

## Supervisory Cycle

1. Load the current goal, Plan IR revision, module states, required gates, budgets, and evidence references.
2. Compare acceptance criteria with available evidence and identify the smallest unmet condition.
3. Prefer Sequential Thinking for Plan IR creation or revision when decomposition, dependencies, or alternatives are unresolved. If it is unavailable, record the same decomposition, dependency, and alternative evidence directly in the Plan IR.
4. Create a bounded `research-loop` prerequisite when a decision depends on a source corpus that cannot fit ordinary discovery context, requires independent cross-validation, or mixes local and web-derived evidence.
5. Consume only the compact, traceable Research Conclusion artifact; do not preload its raw corpus. Route research scope and final integration to PL, technical disputes to TA, and evidence reproducibility checks to QA/SDET.
6. Assign ready modules to independent `module-loop` activations only when write scopes and dependency edges are explicit.
7. Consume durable SQLite events for research readiness, module completion, rework, blocked state, contract changes, and integration readiness.
8. Route requirement decisions to PM, architecture decisions to TA, work decomposition and integration decisions to PL, and independent verification to the declared gate owner.
9. Create integration work only after required module evidence targets compatible Git OIDs.
10. Run system verification, then complete, replan, reactivate an affected module, or block the goal.

## Context Discipline

Compile a fresh minimum context packet for every activation. Include only the target goal and work item, target role and seat, selected skill IDs, relevant SQLite snapshot and messages, verified Git OIDs, selected changed paths, and explicitly required evidence.

Exclude unrelated threads, other work items, unselected skills, whole Serena memories, historical chat, and unreferenced source paths. Fail the activation if the compiled packet exceeds its profile budget or contains out-of-scope material. Serena memory may provide only the named slow-changing project facts; it never carries live task, approval, Git, or message state.

## Contract Change Handling

When a public contract, dependency, authority boundary, write scope, or affected-module set changes:

1. Pause dependent integration.
2. Route the proposal to TA and PL.
3. Identify affected modules and invalidate their dependent gates.
4. Approve, reject, or revise the contract through the responsible authority.
5. Persist a new Plan IR revision before resuming work.

## Completion Gate

Declare `GOAL_COMPLETED` only when every acceptance criterion has evidence, required modules are complete, integration and system verification pass, all required gates target the final integration OID, and no blocking finding or unresolved contract change remains.

Route budget exhaustion, missing authority, or unsafe continuation to `GOAL_BLOCKED`. This skill defines the control loop; it does not grant model, tool, write, or approval authority.
