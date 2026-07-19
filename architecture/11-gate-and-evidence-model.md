# Gate and Evidence Model

## Purpose

Define structured review findings, gate decisions, OID pinning, separation of duty, and deterministic integration eligibility.

## Gate Types

| Gate | Subject | Responsible authority |
|---|---|---|
| Requirement | Scope, acceptance meaning, and completion evidence | PM with PL verification |
| Plan | Dependencies, ownership, skills, scopes, gates, and budgets | PL with TA and affected developer review |
| Architecture | Interfaces, state, compatibility, and technical risk | TA with PL coordination |
| Code review | Correctness, regression, maintainability, and contract implementation | TA or independent developer |
| Quality | Acceptance, regression, failure, recovery, and non-functional evidence | QA/SDET |
| Build | Toolchain, dependency, reproducibility, and artifact integrity | Build/release |
| Release | Install, upgrade, recovery, rollback, and signing policy | Build/release with QA and PL |

## Finding

```json
{
  "finding_id": "F-W42-01",
  "work_item_id": "W-42",
  "revision": 3,
  "gate": "architecture",
  "severity": "high",
  "blocking": true,
  "location": "src/runtime/state_manager.cpp",
  "evidence": "Shutdown callback can reacquire the lifecycle lock.",
  "impact": "Shutdown may deadlock or re-enter lifecycle state.",
  "required_change": "Move callback disposal outside the locked transition.",
  "status": "OPEN",
  "opened_against_oid": "d920f31a82..."
}
```

Finding lifecycle:

```text
OPEN
  -> ADDRESSED
  -> VERIFIED
  -> CLOSED

OPEN
  -> ACCEPTED_RISK

OPEN
  -> REJECTED_AS_INVALID
```

Original evidence is append-only.

## Gate Decision

```json
{
  "decision_id": "DEC-W42-ARCH-R3",
  "gate": "architecture",
  "reviewer_role": "ta",
  "status": "CHANGES_REQUESTED",
  "reviewed_oid": "d920f31a82...",
  "finding_ids": ["F-W42-01"],
  "evidence_refs": [],
  "missing_context": []
}
```

Allowed decision classes:

```text
APPROVED
CHANGES_REQUESTED
NEED_MORE_CONTEXT
REJECTED
PASSED
FAILED
```

Specific gates constrain which status values apply.

## OID Pinning

Every code-sensitive decision records:

- base OID;
- reviewed or tested head OID;
- artifact target OID;
- integration target OID when evaluated.

Core invariant:

```text
approved_oid == tested_oid == integration_target_oid
```

## Separation of Duty

- The author cannot issue final code approval for its own revision.
- Architecture approval does not imply quality approval.
- Quality approval does not imply release approval.
- PL aggregation does not erase independent gate evidence.
- A changed head OID invalidates gates that do not explicitly cover it.

## Gate Aggregator

Inputs:

- required gates from Plan IR;
- target integration OID;
- latest decision per required gate;
- open blocking findings;
- acceptance-evidence mapping;
- policy-required additional approvals.

Output:

```json
{
  "target_oid": "d920f31a82...",
  "eligible": true,
  "required_gates": [],
  "satisfied_gates": [],
  "missing_gates": [],
  "mismatched_oids": [],
  "open_blocking_findings": []
}
```

## Evidence Requirements

Every approval states:

- exact subject and scope;
- target OID;
- reviewer role;
- evidence considered;
- omitted evidence;
- findings;
- residual risk;
- decision time.

Natural-language approval without this record is non-authoritative.

## Current Implementation Status

Specified. Current messages and artifacts can carry gate information, but finding persistence, decision schemas, reviewer-independence checks, OID aggregation, and deterministic gate evaluation are not implemented.

## Consumed By

- [Review, Approval, and Rework](../workflow/10-review-approval-and-rework.md)
- [Integration and Release](../workflow/13-integration-and-release.md)
- [Goal Supervisory Loop](../workflow/12-goal-supervisory-loop.md)
