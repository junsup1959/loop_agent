# Integration and Release

## Purpose

Integrate approved local Git OIDs and verify the exact integration result through build, test, installation, upgrade, recovery, and rollback.

## Architecture Contract

- Integration roles and separation of duty: [Team and Authority Model](../architecture/04-team-and-authority-model.md)
- OID and workspace authority: [Git and Workspace System](../architecture/09-git-and-workspace-system.md)
- Gate aggregation and evidence pinning: [Gate and Evidence Model](../architecture/11-gate-and-evidence-model.md)
- Local-only and privileged-operation policy: [Security and Authority Boundaries](../architecture/14-security-and-authority-boundaries.md)

## Entry Conditions

- Every candidate has required gate approval.
- Candidate approvals and tests target exact candidate OIDs.
- No blocking finding remains.
- Integration order follows the active Plan IR.
- A dedicated integration worktree and build directory exist.

## Integration Workflow

1. Prepare the integration worktree at the approved base.
2. verify candidate OIDs and gate evidence.
3. recompute changed paths and overlap.
4. apply candidates in planned order.
5. stop and create integration work when conflicts occur.
6. produce the integration OID.
7. build from a clean output directory.
8. run required integrated verification.
9. persist evidence against the integration OID.
10. evaluate integration eligibility.

## Release Workflow

1. Pin the release candidate to the integration OID.
2. rebuild with declared toolchain and dependency versions.
3. verify hashes, version stamping, and package contents.
4. run clean installation.
5. run supported upgrades and data migrations.
6. validate repair, recovery, or uninstall as required.
7. validate rollback.
8. execute signing only under explicit authority.
9. create the local release ref.
10. persist release evidence.

## Release Evaluation

The release passes only when all required build, package, install, upgrade, recovery, rollback, and OID checks succeed.

## Failure Routes

| Condition | Route |
|---|---|
| Candidate gate OID mismatch | Reject candidate and rerun gates. |
| Integration conflict | Create integration work item. |
| Integrated regression | Route to owning module loop. |
| Contract conflict | Route to TA and replan. |
| Install or upgrade failure | Build/release rework |
| Rollback failure | Block release. |

## Exit Conditions

- integration or release OID is immutable;
- evidence targets that OID;
- required gates pass;
- local refs and artifacts are durable;
- no remote publication occurred without explicit authority.

## Implementation Status

Specified. Integration controller, gate aggregator, installer validation, and release DAG are not implemented.

## Related Documents

- [Review, Approval, and Rework](10-review-approval-and-rework.md)
- [Goal Supervisory Loop](12-goal-supervisory-loop.md)
- [Failure and Recovery](14-failure-and-recovery.md)
