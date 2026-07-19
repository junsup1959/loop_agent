---
name: map-codebase
description: Map entry points, call paths, state transitions, project boundaries, dependencies, and change impact before editing a large or unfamiliar solution. Use for investigation and context compilation, especially in legacy or multi-project repositories.
---

# Map Codebase

Reduce implementation risk by producing a traceable map of the behavior and ownership boundary.

## Procedure

1. Identify solution files, project files, build targets, and runtime entry points.
2. Trace the user or system trigger through core logic to I/O, UI, device, and persistence boundaries.
3. Record owning files and symbols, state transitions, branch conditions, and side effects.
4. Separate confirmed paths from likely paths and unresolved unknowns.
5. Identify shared abstractions and compatibility surfaces that amplify change impact.
6. Produce the smallest context set required for the next role.

## Source-Analysis Boundary

- Use Serena for targeted semantic source exploration and, when named by the context artifact, targeted project-memory reads.
- This investigative skill does not grant planning, approval, write-scope, workspace, or shared-memory publication authority.
- Do not propose a fix unless the active task explicitly requests one.

## Return Contract

Return:

- ordered primary execution path;
- critical files and symbols by layer;
- state, I/O, concurrency, and compatibility boundaries;
- high-risk branches and shared dependencies;
- confidence-labeled unknowns and the fastest next check;
- recommended Git paths and OIDs for the context packet.

## Authority Boundary

This skill is investigative and read-only by default. It does not grant edit authority, ownership, approval rights, model selection, or tool permissions.
