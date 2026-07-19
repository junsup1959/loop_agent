---
name: modernize-legacy-systems
description: Plan and execute incremental modernization or behavior-preserving refactoring of legacy desktop, service, library, or automation code. Use when unsupported dependencies, brittle seams, or old runtime constraints must be improved without a big-bang rewrite.
---

# Modernize Legacy Systems

Reduce legacy risk through phased, reversible changes that preserve critical behavior and delivery continuity.

## Procedure

1. Map critical behavior, unsupported dependencies, brittle boundaries, and compatibility contracts.
2. Identify missing tests and observability that block safe change.
3. Rank modernization candidates by risk reduction, dependency leverage, and migration cost.
4. Choose an incremental seam: adapter, facade, strangler, parallel run, or localized refactor.
5. Separate behavior-preserving structural changes from feature changes.
6. Define coexistence, migration, rollback, and removal criteria.
7. Validate equivalence on critical normal, failure, and compatibility paths.

## Quality Rules

- Prefer reversible phases over a big-bang rewrite.
- Preserve data formats, interfaces, installation behavior, and previous-version compatibility unless explicitly changed.
- Keep transitional architecture bounded by exit criteria.
- Use small commits that can be reviewed and reverted independently.
- State residual debt that is intentionally deferred.

## Authority Boundary

This skill does not authorize a migration program, broad refactor, compatibility break, model selection, or tool permissions. The active task and assigned organizational authority define scope.
