# Large-Scale Research Loop

## Purpose

Run a bounded, evidence-first research workstream for a question that requires more web, file, or repository material than a normal discovery activation can safely hold in context.

The loop produces a traceable research conclusion for a goal, Plan IR, architecture decision, or module discovery phase. It does not perform implementation or replace the existing review gates.

## Architecture Contract

- Research artifacts, source ledger, context boundary, SQLite routing, and TaskFlow identifiers: [Large-Scale Research Evidence](../architecture/16-large-scale-research-evidence.md)
- Organizational ownership and separation of duty: [Team and Authority Model](../architecture/04-team-and-authority-model.md)
- Plan and TaskFlow responsibility boundaries: [Planning and Orchestration](../architecture/06-planning-and-orchestration.md)
- Context selection and artifact evidence rules: [Context and Evidence System](../architecture/08-context-and-evidence-system.md)

## Entry Conditions

- A bounded question, scope, non-goals, and completion criteria exist.
- The PL accepts ownership of the research workstream.
- A local artifact root and SQLite message database are available.
- The applicable project anchor and Git OIDs are known when the conclusion concerns a codebase revision.
- Source-class, local path, network, sensitivity, license, and retention constraints are explicit.
- The research budget defines source, shard, conflict-round, storage, and deadline limits.

Do not start a large-scale research run merely because more context would be convenient. Use ordinary discovery for a bounded repository path; create this workstream only when evidence collection or comparison must be partitioned and merged.

## State Flow

```text
DEFINE_BRIEF
  -> COLLECT_SOURCES
  -> SHARD_SOURCES
  -> READ_AND_SUMMARIZE
  -> MERGE_CLAIMS
  -> CROSS_VALIDATE
     -> CONFLICT_RESEARCH
        -> CROSS_VALIDATE
     -> SYNTHESIZE_CONCLUSION
        -> RESEARCH_COMPLETED
     -> RESEARCH_BLOCKED
```

The loop is bounded by the approved source, shard, conflict-round, storage, and deadline budgets. A repeated pass must change source coverage, shard boundary, evidence selection, verifier, or question framing; it must not repeat an unchanged prompt.

## Workflow

### 1. Define the Brief

The PM supplies requirement meaning and acceptance criteria. The PL validates the research brief, assigns the research ID, records the project anchor when applicable, and defines the final conclusion format.

The brief must state:

- the exact question and decision it informs;
- scope, non-goals, source classes, authority requirements, and exclusion rules;
- source and artifact budgets;
- target roles, selected skills, independent verification requirement, and conflict owner;
- completion evidence and blocked conditions.

Emit `RESEARCH_REQUESTED` with the research brief artifact reference.

### 2. Collect and Ledger Sources

Collect only sources permitted by the brief. Persist each source as a local artifact and publish a source-ledger record with locator, retrieval time, content type, integrity hash, authority tier, sensitivity, and retention data.

For repository material, pin the evidence to the relevant repository and OID. For web or local-file material, record its own provenance and hash; do not imitate a Git OID or commit downloaded source bodies to Git.

Emit `RESEARCH_SOURCE_READY` only after the source manifest is durable.

### 3. Shard Sources and Bind Lanes

The PL partitions the eligible source set into disjoint, traceable shard manifests. Each manifest names one source range or bounded source group, its artifact references, the question subset, required output, and lane budget.

Bind each shard to an existing seat through a normal work-item revision. There is no permanent researcher seat. Use the smallest number of concurrent lanes that preserves independence and the configured team limit.

Emit `RESEARCH_SHARD_ASSIGNED` with the shard ID and manifest reference. Do not place source text in the message.

### 4. Read and Summarize

Each lane receives only its brief, assigned shard manifest, selected source ranges, bounded role context, selected skills, and required output schema. The full raw source remains in the local artifact store; it is not injected as a complete queue message, TaskFlow value, or general context packet.

For every shard, produce a structured summary artifact containing:

- source and shard IDs, input size, output size, and integrity references;
- the summary body, claims, evidence locators, quotations or source positions when allowed, and uncertainty;
- a `summary_ratio` calculated from the explanatory summary body and the shard input body;
- `compression_target = 0.10` and `compression_over_target` metadata.

The ten-percent ratio is an advisory compression target, not a validity rule. Preserve an over-target summary artifact, record the warning, and later request a narrower artifact range or excerpt if the role context cannot fit. Never reject, truncate, or discard a summary solely because its ratio exceeds ten percent.

Network, storage, and retention policy may explicitly stop a new acquisition before it becomes evidence, but they must never silently retain a partial source as complete. The compiled context packet has a hard selection budget while preserving the authoritative full artifact locally.

Emit `RESEARCH_SHARD_COMPLETED` with the summary artifact reference.

### 5. Merge Claims Instead of Concatenating Summaries

The PL or a specifically assigned merge work item constructs a claim and evidence matrix. Merge normalized claims, evidence locators, scope conditions, confidence, and uncertainties; do not concatenate every summary body into a new prompt.

Each material conclusion claim must retain at least one resolvable artifact reference. The merger must distinguish:

```text
SUPPORTED
CONTRADICTED
INSUFFICIENT_EVIDENCE
OUT_OF_SCOPE
```

Emit `RESEARCH_MERGE_READY` with the claim-matrix artifact reference.

### 6. Cross-Validate and Resolve Conflicts

Detect conflicts when credible evidence supports incompatible claims, a claim lacks the required evidence class, source applicability differs, or a material assertion cannot be reproduced from the referenced artifact.

For each conflict:

1. Persist a conflict artifact with claim IDs, evidence references, scope, and requested decision.
2. Emit `RESEARCH_CONFLICT` only to the producing lane, the designated independent verifier, and the PL.
3. Request the smallest missing source range, evidence locator, or comparison needed to resolve it.
4. Route architecture interpretation to TA, requirement meaning to PM, and evidence reproducibility to QA/SDET when applicable.
5. Record the outcome as `RESOLVED`, `QUALIFIED`, `UNRESOLVED`, or `BLOCKED` and emit `RESEARCH_CONFLICT_RESOLUTION`.

Do not resolve conflicts by majority vote, overwrite the losing claim, or silently remove it. An unresolved conflict remains explicit in the final conclusion.

### 7. Synthesize the Conclusion

The PL synthesizes one conclusion artifact from the claim matrix and resolved conflict records. It must contain:

- direct answer and applicability boundary;
- claim-to-evidence mapping and source coverage;
- alternatives considered and reason for the selected conclusion;
- unresolved conflicts, uncertainties, and residual risk;
- the code anchor OIDs when the conclusion applies to a repository state;
- permitted next action: plan, architecture decision, bounded discovery, module work, or blocked escalation.

Emit `RESEARCH_CONCLUSION_READY` with the conclusion artifact reference. The conclusion is evidence for a subsequent decision; it is not itself a plan, gate approval, or implementation authorization.

## TaskFlow and Message Rules

The research DAG passes only immutable IDs and artifact references between tasks:

```text
research_id
source_id
shard_id
claim_id
conflict_id
artifact_ref
```

Do not send raw source text, full summaries, or unbounded source lists through DagRun configuration, XCom, or SQLite message payloads. Use TaskFlow for fan-out, joins, retries, and scheduling only; use SQLite for durable role routing; use the artifact store for large evidence.

## Interaction with Goal and Module Loops

### Goal Loop

Create a research workstream when source evidence is insufficient for Plan IR creation, dependency reasoning, or a material decision. The goal loop consumes `RESEARCH_CONCLUSION_READY`, then creates or revises the Plan IR using the conclusion artifact reference.

Route a changed public contract, dependency set, authority boundary, or affected-module set through TA and PL replan. Route budget exhaustion or an unresolvable policy constraint to `GOAL_BLOCKED`.

### Module Loop

Request research only while the module is `DISCOVERING` or after `NEED_EVIDENCE`. Keep the module in its existing discovery path while the research workstream runs. On completion, resume discovery or design with a narrowed evidence packet. On a cross-module implication, return `PARENT_REPLAN`; do not use research to change another module contract directly.

## Context Discipline

Every lane, merger, verifier, and conclusion activation receives a fresh minimum context packet. Include only the current action, assigned artifacts, applicable claim or conflict records, selected messages, selected skills, and target OIDs when relevant.

If a full source or summary would exceed the role profile, retain it in the artifact store and request a narrower shard, range, or excerpt. Record the omitted material and selection reason. Do not increase the context budget implicitly and do not preload other lanes' research traffic.

## Exit Gate

Declare `RESEARCH_COMPLETED` only when:

- all required source classes and planned shards have a durable outcome;
- each material conclusion claim resolves to evidence references;
- every detected material conflict is resolved, qualified, or disclosed as unresolved;
- source coverage, omissions, compression warnings, and residual risk are recorded;
- the conclusion identifies the next accountable role and permitted next action; and
- all research budgets remain within policy or an explicit escalation has been recorded.

## Failure Routes

| Condition | Route |
|---|---|
| Source unavailable, disallowed, or unverifiable | Record the limitation and create a bounded replacement-source task or block the affected claim. |
| Source or summary exceeds a context profile | Keep the full artifact, record the omission, and request a narrower shard or excerpt. |
| Summary exceeds the 10 percent target | Retain it, record `compression_over_target`, and narrow later context selection if necessary. |
| Material contradiction | Create a conflict record and route targeted evidence requests through SQLite. |
| Conflict changes project contract or dependencies | Route to TA and PL, then parent replan. |
| Research budget exhausted | Emit a documented incomplete conclusion or `RESEARCH_BLOCKED`; never infer success. |
| Required decision authority absent | Emit `BLOCKED` with the evidence and accountable owner. |

## Implementation Status

Partial. The delivery bundle implements local research-ledger persistence, artifact storage, advisory summary-compression metadata, reference-only context selection, bounded artifact injection, and a dedicated research-iteration TaskFlow DAG. Automatic research-plan compilation, dynamic fan-out, and automatic agent dispatch remain specified work.

## Related Documents

- [Discovery and Source Evidence](02-discovery-and-source-evidence.md)
- [Plan IR and Task DAG](03-plan-ir-and-task-dag.md)
- [Context Compilation](08-context-compilation.md)
- [Module Development Loop](11-module-development-loop.md)
- [Goal Supervisory Loop](12-goal-supervisory-loop.md)
