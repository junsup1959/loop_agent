# Git and Workspace System

## Purpose

Define local Git as the code and immutable evidence authority while isolating concurrent writers by work-item revision.

## Repository Topology

```text
Local Bare Repository
  -> work branches
  -> integration branches
  -> release refs

Worktree Allocator
  -> implementation worktree
  -> detached review worktree
  -> detached QA worktree
  -> integration worktree

Build Allocator
  -> per-work-item output
  -> integration output
  -> release output
```

No remote repository is required.

## Isolation Unit

```text
work item
+ revision
+ branch
+ worktree
+ build output
+ workspace lease
+ write scope
```

## Branch Convention

```text
work/<work-item>/<role>/<revision>
integration/<work-item-or-goal>
release/<goal>/<revision>
```

Branches tied only to a long-lived agent ID are prohibited.

## Writer Ownership

| Workspace type | Mutation authority |
|---|---|
| Work branch and worktree | Assigned implementation role |
| Detached review worktree | Read-only |
| Detached QA worktree | Product source read-only |
| Integration branch and worktree | PL or integration controller |
| Release ref and worktree | Build/release controller after required gates |

## Workspace Lease

```json
{
  "workspace_id": "WS-W42-DEV1-R3",
  "work_item_id": "W-42",
  "revision": 3,
  "owner_role": "dev_1",
  "branch": "work/W-42/dev_1/3",
  "worktree_path": ".agent-team/worktrees/W-42-dev-1-r3",
  "build_path": ".agent-team/build/W-42-dev-1-r3",
  "base_oid": "71ae234f9c...",
  "write_scope": [
    "src/runtime/**",
    "tests/runtime/**"
  ],
  "status": "ACTIVE",
  "lease_until": "timestamp"
}
```

## Write Scope

Read scope permits investigation. Write scope is the maximum path set the assigned role may modify for one work-item revision.

Overlapping parallel write scopes require:

- dependency ordering;
- work-item merge;
- predecessor contract work; or
- explicit PL integration ownership.

## OID Authority

- `base_oid` identifies the immutable starting commit.
- `head_oid` identifies a submitted revision.
- reviewers and QA use detached worktrees at `head_oid`.
- changed paths are recomputed from `base_oid..head_oid`.
- advancing the branch does not update existing approvals.

## Submission Contract

```json
{
  "workspace_id": "WS-W42-DEV1-R3",
  "base_oid": "71ae234f9c...",
  "head_oid": "d920f31a82...",
  "branch": "work/W-42/dev_1/3",
  "changed_paths": [],
  "commit_count": 1
}
```

## Archive and Cleanup

Archive refs preserve:

- submitted revisions used by findings;
- approved integration candidates;
- rejected revisions required by policy;
- release and rollback points.

Cleanup never removes active leases, referenced commits, or the only copy of evidence.

## Current Implementation Status

Specified. Bare-repository registry and OID reading exist. Worktree allocation, workspace leases, branch creation, write-scope enforcement, build allocation, archive policy, and cleanup are not implemented.

## Consumed By

- [Git Workspace Isolation](../workflow/06-git-workspace-isolation.md)
- [Agent Task Execution](../workflow/09-agent-task-execution.md)
- [Integration and Release](../workflow/13-integration-and-release.md)
