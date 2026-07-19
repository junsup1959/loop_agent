# Goal Intake

## Purpose

Convert a human objective into a durable, authority-bounded goal that can be planned without inventing requirements.

## Architecture Contract

Role authority is defined by the [Team and Authority Model](../architecture/04-team-and-authority-model.md). Goal, acceptance, authority, and budget fields are defined by the [Domain and State Model](../architecture/03-domain-and-state-model.md).

## Entry Conditions

- A human or approved upstream system submitted an objective.
- The requesting authority and permitted project scope are known.
- The request has not yet been decomposed into implementation tasks.

## Required Input

```json
{
  "goal_id": "G-001",
  "objective": "Prevent shutdown callback re-entry in the desktop runtime.",
  "requested_by": "local-owner",
  "authority_boundary": {
    "repositories": ["product"],
    "allowed_change_types": ["source", "tests", "build"],
    "forbidden_actions": ["remote-push", "external-release"]
  },
  "constraints": [
    "Preserve the public StateManager contract.",
    "Use local Git only."
  ]
}
```

## Workflow

1. Restate the desired outcome in observable terms.
2. Separate explicit requirements from assumptions and inferred constraints.
3. Define in-scope, out-of-scope, and deferred behavior.
4. Define acceptance criteria for normal, failure, recovery, compatibility, and operational behavior as applicable.
5. Identify decisions that require PM, PL, TA, security, or release authority.
6. Define the budget boundary: maximum plan revisions, module iterations, time, tokens, and artifact retention.
7. Persist the goal and emit `GOAL_DEFINED`.

## Output Contract

```json
{
  "goal_id": "G-001",
  "revision": 1,
  "status": "GOAL_DEFINED",
  "objective": "Prevent shutdown callback re-entry.",
  "in_scope": ["runtime shutdown path", "runtime regression tests"],
  "out_of_scope": ["public API redesign"],
  "acceptance_criteria": [
    {
      "id": "AC-01",
      "statement": "A shutdown callback cannot re-enter StateManager shutdown.",
      "required_evidence": ["automated-test", "code-review"]
    }
  ],
  "assumptions": [],
  "open_decisions": [],
  "budget": {
    "max_plan_revisions": 3,
    "max_module_iterations": 4
  }
}
```

## Exit Gate

The goal may enter discovery only when:

- the outcome is observable;
- scope and authority boundaries are explicit;
- every acceptance criterion names required evidence;
- blocking product decisions are resolved or explicitly routed;
- budget and terminal blocked conditions exist.

## Failure Routes

| Condition | Route |
|---|---|
| Objective is ambiguous | Return `GOAL_NEEDS_CLARIFICATION` to PM. |
| Requested authority exceeds local policy | Return `POLICY_BLOCKED`. |
| Completion cannot be evidenced | Return `GOAL_NOT_VERIFIABLE`. |
| Requirements conflict | Record both claims and route an explicit decision request. |

## Invariants

- Do not create implementation tasks during goal intake.
- Do not convert an assumption into an acceptance criterion without an owner decision.
- Do not assign technical expertise as a permanent role property.

## Implementation Status

Specified. The current SQLite schema does not yet persist goals or acceptance criteria.

## Related Documents

- [Team and Authority Model](../architecture/04-team-and-authority-model.md)
- [Domain and State Model](../architecture/03-domain-and-state-model.md)
- [Discovery and Source Evidence](02-discovery-and-source-evidence.md)
- [Role and Skill Binding](04-role-and-skill-binding.md)
- [Goal Supervisory Loop](12-goal-supervisory-loop.md)
