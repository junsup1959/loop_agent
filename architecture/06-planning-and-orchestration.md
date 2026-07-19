# Planning and Orchestration

## Purpose

Separate evidence gathering, multi-step planning, plan validation, DAG compilation, and task scheduling into distinct interfaces.

## Components

```text
Source Evidence Adapter
  -> Planning Provider
  -> Plan IR
  -> Plan Validator
  -> DAG Compiler
  -> Airflow TaskFlow Runtime
```

## Source Evidence Adapter

Serena is the semantic source-evidence and slow-changing project-knowledge provider when project triggers require:

- source analysis above 50K tokens;
- precise symbol reference and impact tracing;
- multi-file structural or dependency analysis.

All roles may use Serena to locate source, identify symbols and references, analyze structure, return impact evidence, and read only the project-memory references selected for the current activation. Semantic source exploration should be pinned to the relevant repository and target OID before it becomes planning evidence.

Serena tool availability does not decide work allocation, designs, TaskFlow control, message routing, approval, or release. Those remain role- and work-item-governed. The PL alone publishes or refreshes shared Serena project memory; other roles provide evidence-backed proposals through SQLite.

The project runs one shared Serena Streamable HTTP service on a persisted loopback endpoint. The service is prepared by `$serena-project-setup` before `scripts/init_agent_team.py` generates the Codex MCP configuration. Per-agent Serena subprocesses and stdio bindings are not part of the runtime topology.

## Planning Provider

Sequential Thinking is used for:

- work decomposition;
- dependency inference;
- alternative comparison;
- critical-path reasoning;
- write-scope conflict resolution;
- failure-route design;
- replanning.

One configured provider is active for one planning operation. A local package and a local container may be alternative deployments but are not combined into one plan result.

## Plan IR

Plan IR is immutable per revision and owns:

- goal and plan identifiers;
- assumptions and constraints;
- source-evidence references;
- work items;
- role ownership;
- selected skill IDs;
- dependencies;
- read and write scopes;
- input and output contracts;
- required gates;
- failure routes;
- iteration and resource budgets.

## Plan Validator

Validation layers:

```text
Schema
  -> identity and revision
  -> dependency graph
  -> authority
  -> role and skill eligibility
  -> write-scope overlap
  -> separation of duty
  -> evidence contracts
  -> failure routes and budgets
```

Invalid plans never reach TaskFlow compilation.

## DAG Compiler

The compiler:

- topologically orders work items;
- identifies parallel groups;
- materializes joins and gates;
- maps work-item contracts to TaskFlow tasks;
- emits a plan-revision artifact;
- freezes topology before the DagRun.

Material topology change requires a new Plan IR revision and DagRun.

## Airflow Boundary

Airflow owns:

- task ordering;
- parallel scheduling;
- DagRun and task execution records;
- task input and output handoffs;
- infrastructure retries.

Airflow does not own:

- code meaning;
- plan intent;
- role authority;
- skill selection policy;
- context compression;
- technical gate decisions.

## DAG Families

| DAG | Responsibility |
|---|---|
| Goal supervisor | Plan creation, replanning, workstream coordination |
| Module iteration | One durable module-loop iteration |
| Review gate | Context compilation and structured gate decision |
| Integration | Candidate integration and system verification |
| Release | Packaging, installation, upgrade, recovery, and rollback |

## Current Implemented DAG

```text
load_runtime_conf
  -> compile_role_context
  -> execute_role_agent
  -> persist_result_and_messages
```

The current DAG ID is `agent_team_module_iteration`.

## Retry Boundary

Airflow retry keeps the same plan revision, work-item revision, role, skill packet, context input contract, and Git OIDs. Changed evidence or intent creates a new workflow iteration instead.

## Current Implementation Status

Partial. Sequential Thinking and Serena policy boundaries are defined, and one fixed TaskFlow DAG exists. Plan IR schema, validator, general DAG compiler, and the remaining DAG families are not implemented.

## Consumed By

- [Discovery and Source Evidence](../workflow/02-discovery-and-source-evidence.md)
- [Plan IR and Task DAG](../workflow/03-plan-ir-and-task-dag.md)
- [TaskFlow Execution](../workflow/05-taskflow-execution.md)
