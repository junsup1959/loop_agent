# Team and Authority Model

## Purpose

Define stable organizational responsibility and approval authority independently of task-specific technical expertise.

## Team Slots

| Role key | Seat ID prefix | Organizational role | Primary responsibility | Approval authority |
|---|---|---|---|---|
| `pm` | `PM_` | Project Manager | Goal interpretation, scope, priority, acceptance meaning | Requirement and scope gate |
| `pl` | `PL_` | Project Leader and Technical Lead | Task decomposition, ownership, dependencies, integration | Plan and integration gate |
| `ta` | `TA_` | Technical Architect | Solution structure, interfaces, state ownership, compatibility, technical risk | Architecture gate |
| `dev_1` | `DEV_` | Senior Developer Slot | Assigned discovery, implementation, and unit verification | No self-approval |
| `dev_2` | `DEV_` | Senior Developer Slot | Assigned discovery, implementation, and unit verification | No self-approval |
| `dev_3` | `DEV_` | Senior Developer Slot | Assigned discovery, implementation, and unit verification | No self-approval |
| `qa_sdet` | `QA_SDET_` | QA and SDET | Test strategy, automation, regression, failure, performance, and recovery verification | Quality gate |
| `build_release` | `BUILD_RELEASE_` | Build, Release, and Configuration Management | Build, dependency, packaging, installation, update, signing, and rollback | Release gate |

Eight is the maximum logical team size, not a requirement to keep eight model processes running.

## Seat and Role Identity

The project uses two identifiers:

```text
seat_id
= durable human-visible and message-routing identity
= <ROLE_PREFIX>_<generated Korean name>

role_key
= stable ASCII policy key
= role authority, skill eligibility, and shared template binding
```

`scripts/project_agents.py init` randomly generates eight unique Korean names from the project name pool and persists them in `agents/seats/registry.toml`. Validation and synchronization never generate replacement names. `regenerate --confirm-identity-reset` is the only supported identity reset because queued messages and historical artifacts may refer to an existing seat ID.

A seat ID does not imply:

- a resident process;
- permanent technology expertise;
- a long-lived branch;
- unrestricted file ownership;
- authority outside the current goal.

## Separation of Duty

- PM does not approve source correctness.
- PL does not bypass required independent gates.
- TA architecture approval does not replace quality approval.
- A developer does not finally approve its own implementation.
- QA does not silently redefine requirements or architecture.
- Build/release does not waive missing tests.
- Integration checks every required gate against the same target OID.

## Authority Precedence

```text
Human and project policy
  -> Goal authority boundary
  -> Organizational role authority
  -> Work-item ownership and write scope
  -> Selected expertise skills
  -> Agent execution instructions
```

A lower level may narrow permissions but may not expand a higher-level authority boundary.

## Assignment Contract

```json
{
  "work_item_id": "W-42",
  "revision": 3,
  "owner_seat_id": "DEV_<generated-name>",
  "owner_role_key": "dev_1",
  "review_role_keys": ["ta", "qa_sdet"],
  "write_scope": [
    "src/runtime/**",
    "tests/runtime/**"
  ],
  "authority_ref": "GOAL-G001-AUTH-R1"
}
```

## Agent Lifecycle Implication

The system may activate a new model process for every turn. The process resolves `seat_id` to `role_key`, role template, pinned model profile, and skill eligibility from project-local configuration.

## Serena Knowledge Authority

All roles may use Serena for targeted semantic source exploration and for the named project-memory references selected for their activation. This access supports evidence gathering; it does not expand planning, approval, workspace, write-scope, or release authority.

The PL owns the shared Serena project-memory lifecycle:

- publish, refresh, rename, or delete slow-changing project knowledge;
- accept, defer, or reject proposed memory updates after integration evidence is available;
- keep volatile task state, approvals, test results, branches, Git OIDs, and role messages in SQLite, Git, or artifacts instead.

TA, developers, QA/SDET, Build/Release, and PM may submit concise evidence-backed memory proposals through the SQLite message transport. They do not write shared Serena memory directly. Context compilation selects only the references relevant to the current role and action; it never preloads the whole memory set.

## Current Implementation Status

Partial. Six role templates, eight anonymous slot specifications, one-time random Korean identity initialization, a durable seat registry, profile-pinned GPT-5.6 models, self-contained Codex custom-agent compilation, skill-role validation, and separation constraints exist. Queue schemas still use role keys, and the assignment store, authority evaluator, and complete separation-of-duty gate validator are not implemented.

## Consumed By

- [Goal Intake](../workflow/01-goal-intake.md)
- [Role and Skill Binding](../workflow/04-role-and-skill-binding.md)
- [Review, Approval, and Rework](../workflow/10-review-approval-and-rework.md)
