---
name: review-code-quality
description: Review completed code changes for correctness, regression risk, maintainability, contract stability, side effects, and test gaps. Use after implementation and basic tests, or for a read-only audit of an existing diff.
---

# Review Code Quality

Produce evidence-backed findings that are ordered by user impact and practical risk.

## Procedure

1. Read the task contract, base OID, head OID, and changed paths.
2. Map each change to affected behavior and downstream consumers.
3. Check correctness, invariants, errors, state mutation, concurrency, compatibility, and tests.
4. Reproduce or reason through one success path, one failure path, and one integration edge.
5. Classify findings by severity, confidence, and blocking status.
6. Return approval or revision evidence to the designated authority.

## Finding Format

Each finding must include:

- severity and blocking status;
- file and precise location;
- observed evidence;
- failure scenario and user or system impact;
- smallest practical remediation;
- missing validation, if any.

## Quality Rules

- Report defects and material risks, not style preferences.
- Do not inflate severity without probability and blast-radius evidence.
- Keep review independent from implementation authorship when possible.
- Do not edit code unless a separate remediation task grants write ownership.
- Say explicitly when no actionable finding is found.

## Authority Boundary

This skill provides review expertise. It does not grant final technical approval, merge authority, file ownership, model selection, or tool permissions.
