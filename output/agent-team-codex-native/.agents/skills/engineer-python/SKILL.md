---
name: engineer-python
description: Implement and review Python applications, automation, local services, tooling, and tests with attention to runtime behavior, packaging, imports, typing, exceptions, I/O, concurrency, and state consistency. Use when Python owns part of the solution.
---

# Engineer Python

Make narrow, production-oriented Python changes that preserve runtime truth and package behavior.

## Procedure

1. Identify the supported Python versions, entry points, environment, packaging system, and framework conventions.
2. Trace inputs, outputs, exceptions, mutable state, and external I/O.
3. Check import direction, module initialization effects, and dependency boundaries.
4. Implement the smallest coherent change with explicit types where the repository uses them.
5. Validate one success path, one failure path, and one integration boundary.
6. Run the narrow tests and the configured formatter, linter, or type checker when available.

## Technical Focus

- explicit exception contracts and useful diagnostics;
- encoding, filesystem, subprocess, and SQLite behavior;
- thread, process, async task, and cancellation lifecycles;
- packaging metadata, lockfiles, imports, and executable entry points;
- deterministic fixtures and isolation of local state.

## Quality Rules

- Do not suppress type errors that expose runtime uncertainty.
- Avoid import-time side effects and hidden global state.
- Preserve atomicity or transaction-like behavior around stateful I/O.
- Do not perform package-wide style rewrites for a scoped task.

## Authority Boundary

This skill does not grant file ownership, architecture approval, model selection, tool permissions, or dependency-upgrade authority.
