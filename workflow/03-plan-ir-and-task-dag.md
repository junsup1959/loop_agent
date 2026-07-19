# Plan IR and Task DAG

## Purpose

Transform a defined goal and source evidence into a validated, immutable execution graph.

## Architecture Contract

Planning provider boundaries, Plan IR ownership, validation layers, DAG compilation, and Airflow responsibility are defined by [Planning and Orchestration](../architecture/06-planning-and-orchestration.md). Plan, work-item, revision, and budget entities are defined by the [Domain and State Model](../architecture/03-domain-and-state-model.md).

## Entry Conditions

- Goal intake passed.
- Required source evidence exists.
- Blocking requirement and architecture decisions are resolved or represented as predecessor tasks.

## Planning Workflow

1. Invoke Sequential Thinking with the goal, source evidence, constraints, and current durable state.
2. Require a structured Plan IR result.
3. Validate identities, dependencies, authority, scopes, role and skill eligibility, gates, routes, and budgets.
4. Return invalid plans to a new planning revision with validation evidence.
5. Topologically order the validated graph.
6. Compile and persist the immutable DAG definition.

## Validation

Reject a plan containing:

- duplicate work-item IDs;
- missing predecessors;
- dependency cycles;
- overlapping write scopes without ordering or explicit integration ownership;
- an ineligible role or skill;
- self-approval;
- missing input or output contracts;
- missing review gates;
- missing failure routes;
- unbounded iteration or retry;
- actions outside the goal authority boundary.

## DAG Compilation

```text
Plan IR
  -> Schema Validation
  -> Authority Validation
  -> Dependency Validation
  -> Write-Scope Conflict Validation
  -> Skill Selection Validation
  -> Topological Ordering
  -> TaskFlow Definition
  -> Immutable Plan Revision
```

The DAG topology is frozen before a DagRun starts. A material plan change creates a new Plan IR revision and a new DagRun.

## Output Contract

```json
{
  "plan_id": "PLAN-G001-R1",
  "status": "VALIDATED",
  "dag_definition_ref": "artifact://plans/PLAN-G001-R1/dag.json",
  "work_item_count": 3,
  "parallel_groups": [["W-42", "W-43"]],
  "critical_path": ["W-42", "W-50"],
  "validation_artifact": "artifact://plans/PLAN-G001-R1/validation.json"
}
```

## Retry and Replan Boundary

- Retry re-executes the same task with the same input contract after an infrastructure failure.
- Replan changes the Plan IR revision after new evidence, a dependency change, a contract conflict, or a repeated failure.
- Review rework changes a work-item revision without mutating the active DAG topology.

## Implementation Status

Specified. The project does not yet contain the Plan IR schema, validator, or general DAG compiler. The existing TaskFlow script implements one fixed module-iteration graph.

## Related Documents

- [Planning and Orchestration](../architecture/06-planning-and-orchestration.md)
- [Domain and State Model](../architecture/03-domain-and-state-model.md)
- [Role and Skill Binding](04-role-and-skill-binding.md)
- [TaskFlow Execution](05-taskflow-execution.md)
- [Failure and Recovery](14-failure-and-recovery.md)
