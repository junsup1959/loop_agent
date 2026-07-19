# Git Workspace Isolation

## Purpose

Allocate and retire isolated local Git and build state for one mutable work-item revision.

## Architecture Contract

- Repository topology, branch rules, worktree types, leases, write scopes, and OID authority: [Git and Workspace System](../architecture/09-git-and-workspace-system.md)
- Canonical project and runtime paths: [Project Layout](../architecture/02-project-layout.md)
- Path and permission boundaries: [Security and Authority Boundaries](../architecture/14-security-and-authority-boundaries.md)

## Entry Conditions

- The work-item revision, owner role, base OID, and read and write scopes are fixed.
- The local bare repository resolves from the repository registry.
- No active lease conflicts with the requested workspace.

## Allocation Workflow

1. Verify the base OID.
2. Check active workspace and write-scope leases.
3. Create the work branch from the exact base OID.
4. Create dedicated worktree and build-output directories.
5. Persist the workspace lease.
6. Pass only allocated paths to the agent runner.
7. Require at least one commit for a submitted code revision.
8. Resolve the submitted head OID.
9. Recompute changed paths and verify write scope.
10. Create detached review and QA worktrees at the submitted OID.

## Approval Pinning

If the work branch advances:

1. preserve the previous decision against its original OID;
2. mark the new OID unapproved;
3. compile a new delta context;
4. run every affected gate again.

## Cleanup Workflow

1. Verify no active task uses the worktree.
2. Verify the workspace lease is released or expired.
3. Persist required artifacts.
4. Archive required submitted, approved, or rejected refs.
5. remove disposable worktrees and build outputs.
6. retain evidence according to policy.

## Failure Routes

| Condition | Route |
|---|---|
| Base OID missing | `CONTEXT_SOURCE_MISSING` |
| Active conflicting lease | Wait, reconcile, or reassign. |
| Write-scope violation | Stop and emit `POLICY_BLOCKED`. |
| Dirty review worktree | Recreate a detached worktree. |
| Integration conflict | Create a dedicated integration work item. |

## Exit Conditions

- the writer has one isolated mutable worktree;
- review and QA use immutable detached worktrees;
- submitted OIDs and changed paths are verified;
- cleanup cannot remove referenced evidence.

## Implementation Status

Specified. OID verification and bare-repository reads exist. Allocation, leases, scope enforcement, archive, and cleanup are not implemented.

## Related Documents

- [Context Compilation](08-context-compilation.md)
- [Review, Approval, and Rework](10-review-approval-and-rework.md)
- [Integration and Release](13-integration-and-release.md)
