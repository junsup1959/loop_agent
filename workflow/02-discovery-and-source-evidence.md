# Discovery and Source Evidence

## Purpose

Create a read-only, confidence-labeled map of the affected solution before planning or editing.

## Architecture Contract

Role authority comes from the [Team and Authority Model](../architecture/04-team-and-authority-model.md). Serena, Sequential Thinking, and source-evidence component boundaries are defined by [Planning and Orchestration](../architecture/06-planning-and-orchestration.md). Evidence assembly is defined by the [Context and Evidence System](../architecture/08-context-and-evidence-system.md).

## Entry Conditions

- The goal is defined.
- The repository identifier and local repository location are known.
- Discovery questions are bounded by the goal scope.

## Tool Trigger Precedence

1. If a Serena trigger applies, use the shared Serena endpoint for targeted semantic source evidence.
2. Otherwise, use normal repository inspection.
3. Use Sequential Thinking only after evidence collection when decomposition, dependency reasoning, or alternative comparison is required.

Serena-only triggers:

- source ingestion or analysis exceeds 50K tokens;
- a specific function or class requires precise reference and impact tracing;
- multi-file symbol or structural dependency analysis is required.

All roles may use Serena for targeted symbols, references, structure, dependencies, and impact exploration. Tool access does not grant planning, editing, routing, approval, or release authority; those remain controlled by the active role and work-item contract. Do not start a per-agent Serena server: every role connects to the project-shared loopback Streamable HTTP endpoint.

Read only the Serena memory references selected for the discovery activation. Discovery findings are pinned evidence artifacts and SQLite messages, not automatic Serena-memory updates. The PL alone decides whether slow-changing discovery knowledge is published to shared Serena memory after integration evidence is available.

## Workflow

1. Identify solution files, project files, targets, entry points, and external libraries.
2. Trace the relevant user or system trigger through UI, services, state, persistence, device, and native boundaries.
3. Record owning files, symbols, state transitions, concurrency boundaries, side effects, and compatibility surfaces.
4. Separate confirmed paths, likely paths, and unresolved unknowns.
5. Identify tests, build targets, installers, migrations, and recovery paths affected by the proposed behavior.
6. Pin source evidence to a repository and commit OID.
7. Persist the evidence artifact and emit `DISCOVERY_COMPLETED`.

## Evidence Contract

```json
{
  "evidence_id": "SRC-G001-01",
  "goal_id": "G-001",
  "repo_id": "product",
  "source_oid": "71ae234f9c...",
  "entry_points": [],
  "primary_path": [],
  "files": [],
  "symbols": [],
  "state_transitions": [],
  "side_effect_boundaries": [],
  "compatibility_surfaces": [],
  "affected_tests": [],
  "unknowns": [
    {
      "question": "Which component owns callback disposal?",
      "confidence": "low",
      "next_check": "Trace StateManager callback registrations."
    }
  ]
}
```

## Exit Gate

Discovery is sufficient when the planner can identify:

- the primary owning path;
- material dependency and state boundaries;
- write-scope candidates;
- affected validation paths;
- unresolved unknowns that must become explicit discovery tasks.

## Failure Routes

| Condition | Route |
|---|---|
| Commit cannot be resolved | `CONTEXT_SOURCE_MISSING` |
| Evidence is too broad | Split discovery by solution or behavior boundary. |
| Confidence is insufficient | Create a bounded follow-up discovery task. |
| Tool cannot analyze the language | Record the limitation and use the approved fallback analyzer. |

## Invariants

- Source evidence must name its commit OID.
- Discovery output must not contain unapproved implementation decisions.
- A likely path must never be labeled confirmed.

## Implementation Status

Partial. Git OID, changed-path, diff, commit-series, and message context reconstruction are implemented in `scripts/agent_team_context.py`. Semantic symbol indexing and dedicated source-evidence persistence are not complete.

## Related Documents

- [Planning and Orchestration](../architecture/06-planning-and-orchestration.md)
- [Context and Evidence System](../architecture/08-context-and-evidence-system.md)
- [Plan IR and Task DAG](03-plan-ir-and-task-dag.md)
- [Context Compilation](08-context-compilation.md)
- [Git Workspace Isolation](06-git-workspace-isolation.md)
