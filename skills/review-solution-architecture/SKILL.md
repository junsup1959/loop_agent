---
name: review-solution-architecture
description: Review solution structure, project dependencies, module boundaries, state ownership, interfaces, failure isolation, compatibility, and migration risk. Use when a design or change affects multiple projects or long-lived contracts.
---

# Review Solution Architecture

Assess architecture against concrete solution evidence and recommend the smallest change that reduces material risk.

## Procedure

1. Define the affected behavior and system boundary.
2. Map project and module dependencies, interface direction, and state ownership.
3. Evaluate coupling, cohesion, failure propagation, concurrency, and lifecycle behavior.
4. Check data-format, ABI, API, installation, and previous-version compatibility.
5. Compare viable alternatives by risk, complexity, migration cost, and reversibility.
6. Define design decisions, constraints, and validation required before implementation.

## Quality Rules

- Tie every finding to source, build, runtime, or artifact evidence.
- Prioritize critical-path risks over style preferences.
- Preserve existing architecture unless the scoped problem requires a boundary change.
- Include rollout, rollback, and coexistence implications.
- Keep assumptions explicit when runtime evidence is unavailable.

## Return Contract

Return:

- architecture scope and dependency map;
- findings with evidence and impact;
- selected design and rejected alternatives;
- interface, state, lifecycle, and compatibility decisions;
- implementation constraints and validation gates;
- residual risks and open decisions.

## Authority Boundary

This skill supplies architectural expertise but does not itself approve a design. Approval remains with the assigned TA or PL. It does not grant edit authority, model selection, or tool permissions.
