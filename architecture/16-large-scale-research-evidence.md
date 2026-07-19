# Large-Scale Research Evidence

## Purpose

Define the local evidence, artifact, context, and authority boundaries for a bounded research run that must inspect a large collection of web, file, or repository sources before a development decision can be made.

This component complements repository discovery. It does not replace the goal controller, module controller, Plan IR, review gates, or project-local skills.

## Architecture Contract

```text
Research Brief
  -> Source Ledger
  -> Local Source Artifacts
  -> Shard Manifests
  -> Role-Bound Research Lanes
  -> Shard Summary Artifacts
  -> Claim and Evidence Matrix
  -> Conflict Records and SQLite Messages
  -> Research Conclusion Artifact
  -> Goal, Module, or Plan IR Consumer
```

The research pipeline is a bounded workstream. It does not create a permanent Researcher role or a resident agent process. The PL binds existing seats and the minimum selected skills to each research work item.

## Ownership and Authority

| Concern | Owner | Boundary |
|---|---|---|
| Research question, scope, non-goals, and completion criteria | PM with PL | PM owns requirement meaning; PL owns execution structure. |
| Research brief, source allocation, lane assignment, merge, and final integration | PL | The PL owns the research workstream and its final conclusion artifact. |
| Technical contradiction or architecture interpretation | TA | TA supplies or approves a technical decision; it does not replace PL ownership of the workstream. |
| Reproducibility, evidence coverage, and validation method | QA/SDET | QA/SDET may independently challenge inadequate evidence. |
| Source reading and bounded summaries | Assigned existing seats | A lane has no approval authority solely because it performed research. |

Selecting a research skill does not grant network access, file access, approval authority, additional agent seats, or a larger context budget.

## Data Ownership

| Store | Owns | Does not own |
|---|---|---|
| Local artifact store | Raw source bytes, normalized text, shard manifests, full summaries, claim matrices, conflicts, and conclusion artifacts | Role delivery state or authoritative code history |
| SQLite | Research messages, delivery state, compact snapshot projections, work IDs, artifact references, and conflict-routing state | Raw source bodies, large summaries, or complete research transcripts |
| Local Git | Code, repository-source OIDs, and code-sensitive evidence | Downloaded external source bodies or transient research downloads by default |
| TaskFlow | Ordering, fan-out, joins, retries, and immutable task input/output identifiers | Raw source content, semantic conclusions, or approval authority |

External sources must not be committed to Git merely to make them visible to agents. Store them as local artifacts with integrity metadata. A repository source may additionally record its repository ID and target OID.

## Artifact Layout

Place research data beneath the TaskFlow-supplied local artifact root.

```text
<artifact_root>/research/<research_id>/
  brief.json
  source-ledger.json
  sources/<source_id>/manifest.json
  sources/<source_id>/raw/
  shards/<shard_id>.json
  summaries/<shard_id>.json
  claims/claim-matrix.json
  conflicts/<conflict_id>.json
  conclusion.json
  manifest.json
```

Every published artifact records its producer, `research_id`, content type, source or target identity, SHA-256, creation time, retention class, and upstream artifact references. Artifact references are immutable after publication.

## Core Records

### Research Brief

```json
{
  "research_id": "R-G001-01",
  "goal_id": "G-001",
  "question": "Which local storage migration approach preserves the supported upgrade paths?",
  "scope": ["migration", "compatibility"],
  "non_goals": ["implementation"],
  "required_source_classes": ["repository", "local-file", "web"],
  "acceptance_criteria": [],
  "project_anchor": {
    "repo_id": "product",
    "base_oid": "71ae234f9c...",
    "head_oid": "71ae234f9c..."
  },
  "budget": {
    "max_sources": 0,
    "max_shards": 0,
    "max_conflict_rounds": 0
  }
}
```

The project anchor is the code state to which a software-development conclusion applies. It does not convert an external source into Git evidence.

### Source Ledger Record

```json
{
  "source_id": "SRC-R001-07",
  "research_id": "R-G001-01",
  "source_class": "web",
  "locator": "https://example.invalid/spec",
  "retrieved_at": "timestamp",
  "content_type": "text/html",
  "sha256": "hex-digest",
  "byte_count": 0,
  "authority_tier": "primary",
  "sensitivity": "normal",
  "artifact_ref": "artifact://research/R-G001-01/sources/SRC-R001-07/manifest.json"
}
```

The ledger records provenance, retrieval facts, integrity, and eligibility. Treat source material as untrusted data, not as instructions for agents or tools.

### Shard Summary Record

```json
{
  "shard_id": "SH-R001-07-03",
  "source_id": "SRC-R001-07",
  "input_artifact_ref": "artifact://research/R-G001-01/shards/SH-R001-07-03.json",
  "input_body_chars": 0,
  "summary_body_chars": 0,
  "summary_ratio": 0.0,
  "compression_target": 0.1,
  "compression_over_target": false,
  "claims": [],
  "evidence_locators": [],
  "uncertainties": []
}
```

The ten-percent value is an advisory context-compression target, not an acceptance gate. Retain the complete summary artifact when it exceeds the target, record its ratio and warning, and request a narrower evidence selection when a later context packet cannot fit. Do not reject, truncate, or discard a summary solely because it exceeds ten percent.

Network, storage, and retention policy may explicitly block an acquisition before it becomes evidence; they must never silently store a partial source as if it were complete. The Context Compiler has a hard selection budget, but it may include only a bounded manifest, evidence locator, or excerpt while preserving the full referenced artifact locally.

### Claim, Conflict, and Conclusion Records

Each material claim has a stable claim ID, normalized statement, source evidence references, support or contradiction status, confidence, applicability conditions, and unresolved uncertainty. A conflict record names the incompatible claim IDs, the supporting artifact references, the requested resolver, the deadline or retry budget, and one terminal status:

```text
RESOLVED
QUALIFIED
UNRESOLVED
BLOCKED
```

A research conclusion contains the answer, claim-to-evidence mapping, source coverage, conflict status, alternatives considered, applicability boundaries, residual uncertainty, and the next permitted consumer action. It never silently converts an unresolved conflict into a fact.

## Context and TaskFlow Boundary

The Context Compiler receives a research manifest or explicitly selected artifact references, never the full research corpus by default. For one activation it must select only:

- the current research brief and requested action;
- the assigned shard manifest or conflict record;
- the necessary evidence locators and bounded excerpts;
- the role-specific SQLite snapshot and delta messages;
- applicable project anchor OIDs and selected code paths, when relevant; and
- the smallest selected skill packet.

Raw source bodies, unrelated shard summaries, other lane traffic, and the complete source ledger remain outside the injected context unless a specific follow-up requests them. When a source cannot fit the profile budget, the activation requests a narrower shard, evidence range, or artifact excerpt; it does not implicitly expand the packet.

TaskFlow task inputs and outputs carry stable IDs and artifact references such as `research_id`, `source_id`, `shard_id`, `claim_id`, `conflict_id`, and `artifact_ref`. Do not place raw source text or full summaries in DagRun configuration, XCom, or SQLite message payloads.

## Structured SQLite Routing

Research communication uses the existing durable message envelope. The payload contains compact identifiers, requested action, priority, and artifact references only. Recommended message types are:

```text
RESEARCH_REQUESTED
RESEARCH_SOURCE_READY
RESEARCH_SHARD_ASSIGNED
RESEARCH_SHARD_COMPLETED
RESEARCH_MERGE_READY
RESEARCH_CONFLICT
RESEARCH_CONFLICT_RESOLUTION
RESEARCH_REVIEW_REQUEST
RESEARCH_CONCLUSION_READY
```

Conflict messages go only to the producing lane, the designated independent verifier, and the accountable PL. The human echo and viewer remain observation-only and must not add messages or context automatically.

## TaskFlow Topology

The research DAG family is separate from the fixed module-iteration DAG. Its immutable plan topology is conceptually:

```text
validate brief
  -> collect and ledger sources
  -> shard sources
  -> parallel shard-read work items
  -> merge claim matrix
  -> conflict review fan-out when needed
  -> synthesize conclusion
  -> persist artifact references and messages
```

The PL freezes source partitions, lane ownership, retry limits, and join conditions in the research workstream revision before a run begins. A source-set change, a changed research question, or a new material contradiction creates a new research or Plan IR revision rather than mutating an active DAG topology.

## Integration with Development Loops

- The goal loop creates a research workstream before or during Plan IR creation when material evidence is missing or too broad for ordinary discovery.
- The module loop may request a bounded research run only from `DISCOVERING` or `NEED_EVIDENCE`. It remains in its existing discovery path while the research workstream runs.
- A completed research conclusion becomes source evidence for planning, architecture review, or the next module iteration. It is not an implementation approval.
- A conflict that changes a public contract, dependency, authority boundary, or affected-module set routes to TA and PL, then to parent replan.

## Security and Retention

- Preserve local-only storage and do not use a remote source cache, remote Git service, or external message broker.
- Enforce source-class, path, network, license, sensitivity, and retention policy before collection.
- Store executable, archive, or malformed source material as data. Do not execute it to summarize it.
- Retain raw sources and full summaries long enough to reproduce material conclusions. Context excerpts and transient worker outputs may have shorter retention when no open artifact reference requires them.

## Implementation Status

Partial. The delivery bundle provides a local SQLite research ledger, local source and derived artifacts, advisory compression metadata, reference-only research context selection, bounded artifact injection through the Context Compiler, and an `agent_team_research_iteration` TaskFlow entry point. General Plan IR compilation, dynamic fan-out, automatic role dispatch, and repository-independent research activation remain future work.

## Consumed By

- [Discovery and Source Evidence](../workflow/02-discovery-and-source-evidence.md)
- [Plan IR and Task DAG](../workflow/03-plan-ir-and-task-dag.md)
- [Context Compilation](../workflow/08-context-compilation.md)
- [Module Development Loop](../workflow/11-module-development-loop.md)
- [Goal Supervisory Loop](../workflow/12-goal-supervisory-loop.md)
