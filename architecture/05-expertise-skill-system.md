# Expertise Skill System

## Purpose

Provide task-specific professional expertise without changing organizational responsibility, authority, model permissions, or workspace ownership.

## Architectural Rule

```text
Role
= responsibility, accountability, and approval authority

Skill
= professional methods and technical expertise for the current task
```

Skills are self-contained project artifacts. They do not load global agent definitions or external reference files at runtime.

## Package Structure

```text
skills/
  catalog.toml
  <skill-id>/
    SKILL.md
    agents/
      openai.yaml

.codex/
  expertise-catalog.toml
  skills/
    <skill-id>/
      SKILL.md
      agents/
        openai.yaml
```

`skills/` is canonical. `.codex/skills/` is the generated project runtime mirror.

## Catalog Contract

The catalog owns:

- project scope;
- source and runtime roots;
- allowed organizational roles;
- explicit-injection policy;
- maximum skills per task;
- maximum technology skills per task;
- skill ID, kind, eligible roles, and summary.

Current limits:

```text
explicit injection only
maximum total skills per task = 4
maximum technology skills per task = 2
```

## Skill Kinds

### Workflow expertise

Professional method applied across technologies, such as:

- requirement clarification;
- delivery planning;
- task DAG coordination;
- codebase mapping;
- architecture review;
- debugging;
- code review;
- test engineering;
- build and release;
- legacy modernization.

### Technology expertise

Platform or language expertise, such as:

- C++ systems;
- .NET desktop;
- Python;
- Rust systems;
- PowerShell;
- local data;
- Electron desktop;
- desktop UI;
- embedded devices.

## Package Contract

`SKILL.md` contains:

- precise trigger description;
- procedure;
- technical or professional focus;
- quality rules;
- output expectations where needed;
- explicit authority boundary.

`agents/openai.yaml` contains:

- display metadata;
- a default prompt that explicitly names the skill;
- `allow_implicit_invocation: false`.

No `references` directory is used.

## Resolver Interface

Input:

```json
{
  "seat_id": "DEV_<generated-name>",
  "skill_ids": [
    "map-codebase",
    "engineer-dotnet-desktop",
    "engineer-local-data",
    "engineer-test-coverage"
  ]
}
```

Output:

```json
{
  "scope": "project",
  "seat_id": "DEV_<generated-name>",
  "role_key": "dev_1",
  "agent_file": ".codex/agents/DEV_<generated-name>.toml",
  "model": "gpt-5.6-terra",
  "skill_packet": {
    "explicit_injection": true,
    "skills": [
      {
        "id": "map-codebase",
        "kind": "workflow",
        "skill_md": ".codex/skills/map-codebase/SKILL.md"
      }
    ]
  }
}
```

The agent resolver maps the durable `seat_id` to its ASCII `role_key` before calling the skill resolver. Korean display identity therefore never changes catalog eligibility rules.

## Validation Invariants

- Every catalog entry has one matching package.
- Every package name matches its frontmatter name.
- Skill content and metadata are UTF-8 and English.
- Implicit invocation is disabled.
- The default prompt explicitly names the skill.
- No skill contains unresolved template markers.
- No skill uses a global runtime path.
- No skill package contains a `references` directory.
- The role is eligible for every selected skill.
- Selection limits are enforced before agent execution.

## Synchronization

`scripts/project_skills.py sync` copies canonical packages and catalog data into the project runtime mirror and verifies byte equality for required files.

The runtime mirror is never the independent source of truth.

## Authority Boundary

Selecting a skill does not grant:

- product or technical approval;
- file ownership;
- a broader write scope;
- model choice;
- tool permissions;
- network or administrative access;
- permission to change public contracts.

## Current Implementation Status

Partial. Nineteen project-local packages, catalog validation, synchronization, seat-to-role mapping, role filtering, explicit resolution, selection limits, and tests exist. Work-item persistence and runner-side content injection are not connected.

## Consumed By

- [Role and Skill Binding](../workflow/04-role-and-skill-binding.md)
- [Context Compilation](../workflow/08-context-compilation.md)
- [Agent Task Execution](../workflow/09-agent-task-execution.md)
