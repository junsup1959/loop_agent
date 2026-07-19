# Security and Authority Boundaries

## Purpose

Define trust boundaries and deny implicit authority expansion across roles, skills, model processes, workspaces, local tools, credentials, and release operations.

## Trust Boundaries

```text
Human and Project Policy
  -> Goal Authority
  -> Role Authority
  -> Work-Item Scope
  -> Workspace and Tool Permissions
  -> Model Process

Untrusted or advisory input:
  - model output
  - agent-provided changed paths
  - free-form summaries
  - external dependency metadata
  - generated scripts before review
```

## Local-Only Policy

By default:

- no remote Git push;
- no remote pull-request service;
- no external artifact upload;
- no SaaS queue;
- no cloud deployment;
- no external message delivery;
- no global agent or skill dependency.

External model or MCP access is governed separately and does not change local storage authority.

## Permission Sources

Authority may come only from:

- explicit human instruction;
- project policy;
- goal authority boundary;
- assigned organizational role;
- work-item read and write scopes;
- approved tool and runtime configuration.

Skills and model suggestions are never permission sources.

## Workspace Security

- Resolve and validate absolute workspace paths.
- Reject traversal outside allocated roots.
- Enforce one writer per branch and worktree.
- Keep review and QA source worktrees read-only.
- Do not run untrusted build output as an administrator.
- Verify write scope from Git after execution.

## Message Security

- Store no secrets in message payloads.
- Store no source code as the authoritative message content.
- Treat sender and recipient role fields as claims until validated by the activation controller.
- Use deduplication and idempotent consumers.
- Keep human echo output free of sensitive values.

## Skill Security

- Skills are project-local and English.
- Implicit invocation is disabled.
- Skill packages contain no runtime references directory.
- Skill selection is catalog-validated.
- Skills cannot grant tools, network access, elevation, or approval.

## Privileged Operations

Require separate explicit authority for:

- code signing;
- installer elevation;
- registry or system-wide configuration changes;
- device firmware updates;
- destructive data migration;
- credential access;
- external network publication;
- release promotion.

## Evidence Validation

- Resolve Git OIDs independently.
- Recompute changed paths from Git.
- Verify artifact hashes when promoted.
- Pin gate decisions to OIDs.
- Do not accept natural-language approval as a substitute for a decision record.
- Preserve failed and blocked evidence for audit.

## Secrets

- Keep secrets out of repository files, SQLite payloads, human logs, and model prompts unless explicitly required.
- Use environment or operating-system credential facilities for runtime secrets.
- Redact subprocess diagnostics before human echo or model reuse.
- Rotate secrets after suspected exposure.

## Current Implementation Status

Specified. Several policy boundaries are encoded in skills and documentation, and path or OID validation exists in specific scripts. A unified authority evaluator, role authentication, workspace permission controller, secret-redaction layer, and privileged-action approval engine are not implemented.

## Consumed By

- [Goal Intake](../workflow/01-goal-intake.md)
- [Agent Task Execution](../workflow/09-agent-task-execution.md)
- [Integration and Release](../workflow/13-integration-and-release.md)
