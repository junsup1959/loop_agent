# Context and Evidence System

## Purpose

Define how the system reconstructs a bounded, role-specific decision packet from durable messages, immutable Git state, project-local skills, semantic evidence, and local artifacts.

## Components

```text
Repository Registry
  -> Git Context Reader

SQLite Queue
  -> Snapshot and Delta Reader

Skill Resolver
  -> Project-Local Skill Packet

Artifact Resolver
  -> Build, Test, Review, and Analysis Evidence

Semantic Index Adapter
  -> Symbol and Dependency Evidence

Serena Project-Memory Adapter
  -> Selected Named Knowledge References

Role Lens and Budgeter
  -> Context Packet

Context Profile Catalog
  -> Fail-Closed Selection Caps
```

## Repository Registry

Maps `repo_id` to:

- local bare repository;
- default integration branch;
- optional semantic index path.

Paths may be absolute or registry-relative and must resolve locally.

## Git Context Reader

For a base and head OID, it:

- verifies both as commits;
- computes rename-aware changed paths;
- computes diff statistics;
- returns ordered commits in `base..head`;
- generates a function-context diff;
- truncates the diff to a configured bound;
- discloses truncation.

Agent-provided changed paths are advisory. Git output is authoritative.

## Message Context

The message projection contains:

- latest target-role snapshot;
- messages after the snapshot sequence;
- unresolved required action;
- related decision and finding references when the full schema exists.

## Skill Context

The resolver supplies validated project-local `SKILL.md` paths. Selected skill content is inserted after role and task authority instructions and before evidence.

## Artifact Evidence

Artifact metadata should include:

- artifact ID or URI;
- producer component and role;
- content type;
- goal, plan, work-item, and revision IDs;
- target Git OID where applicable;
- local path;
- integrity hash;
- creation and retention timestamps.

## Semantic Evidence

Serena is a semantic-evidence adapter for targeted symbol, reference, structure, dependency, and impact exploration. A query result becomes reusable evidence only when its artifact records the repository, target OID, selected paths or symbols, producer, and integrity metadata. The compiler references that bounded artifact rather than rediscovering or embedding unrelated source.

All roles may request targeted Serena exploration. The PL alone publishes or refreshes shared Serena project memory. Serena memory stores slow-changing project knowledge; it is not a task-state store and must not contain current approvals, test results, branches, Git OIDs, or agent messages.

For each activation, the compiler selects at most the named Serena memory references required by the role and action. It does not preload every Serena memory or inject entire memory bodies merely because they exist.

Optional adapters:

| Technology | Adapter |
|---|---|
| C and C++ | clangd, libclang, compile commands |
| C# | Roslyn |
| Rust | rust-analyzer |
| Python | AST and Pyright |
| Java and Kotlin | JDT and Kotlin analysis |
| General fallback | Tree-sitter and repository search |

Semantic analysis runs on a detached worktree pinned to the target OID.

## Role Lenses

| Role | Primary evidence |
|---|---|
| PM | Goal, scope, acceptance criteria, open product decisions |
| PL | Plan, dependencies, ownership, gates, integration state |
| TA | Interfaces, state transitions, references, compatibility, architecture risks |
| Developer | Work-item contract, approved design, owning source, failing tests |
| QA/SDET | Acceptance criteria, behavior delta, risk paths, test environment |
| Build/release | Build graph, dependencies, package, install, recovery, rollback |

## Context Packet Contract

```json
{
  "thread_id": "thread-W42",
  "work_item_id": "W-42",
  "target_role": "ta",
  "context_profile": "architecture-review",
  "context_budget": {},
  "skill_packet": {
    "explicit_injection": true,
    "skills": []
  },
  "message_context": {
    "snapshot": {},
    "delta_messages": [],
    "selected_message_ids": []
  },
  "git_context": {
    "repo_id": "product",
    "base_oid": "71ae234f9c...",
    "head_oid": "d920f31a82...",
    "changed_paths": [],
    "selected_paths": [],
    "omitted_changed_path_count": 0,
    "diff_stat": "",
    "commit_series": [],
    "diff": "",
    "diff_truncated": false
  },
  "artifact_refs": [],
  "serena_context": {
    "target_oid": "d920f31a82...",
    "memory_refs": [],
    "semantic_evidence_refs": []
  },
  "semantic_evidence": [],
  "omitted_context": []
}
```

## Non-Compressible Data

- approved requirements and acceptance criteria;
- open questions and findings;
- architecture and contract decisions;
- assigned role and selected skills;
- approval and test target OIDs;
- required next action;
- iteration and budget limits;
- omitted-context disclosure.

## Context Expansion Levels

```text
Level 1: changed hunk and containing symbol
Level 2: direct callers and callees
Level 3: public interfaces, implementations, and affected tests
Level 4: reviewer-requested evidence
```

## Current Implementation Status

Partial. Repository registry, Git context reader, work-item-scoped snapshot and delta reader, fail-closed role budgets, bounded path/diff/commit selection, immutable context artifact persistence, omitted-context disclosure, seat-validated skill selection, and runner-side selected-skill materialization exist. Serena knowledge policy and state helpers exist, but artifact resolution and compiler-connected semantic adapters are not complete.

## Consumed By

- [Discovery and Source Evidence](../workflow/02-discovery-and-source-evidence.md)
- [Context Compilation](../workflow/08-context-compilation.md)
- [Review, Approval, and Rework](../workflow/10-review-approval-and-rework.md)
