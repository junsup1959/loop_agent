---
name: engineer-build-release
description: Engineer deterministic builds, dependency changes, packaging, installation, upgrades, signing, local release integration, and rollback validation for desktop or offline software. Use when delivery artifacts or build graphs change.
---

# Engineer Build and Release

Protect reproducibility, installability, upgrade safety, and recoverability from source to local release artifact.

## Procedure

1. Map solution targets, build graph, toolchains, dependencies, and artifact outputs.
2. Reproduce the affected build or packaging path.
3. Make the smallest coherent build, dependency, installer, or release change.
4. Verify deterministic inputs, version stamping, and artifact integrity.
5. Test clean install, upgrade, uninstall or recovery, and rollback paths as applicable.
6. Record signing, privileged operation, and target-host checks that cannot run locally.

## Quality Rules

- Keep local and automated build assumptions explicit.
- Preserve lockfile and dependency resolver integrity.
- Assess direct and transitive dependency compatibility.
- Use immutable Git OIDs and local artifacts for handoffs.
- Never assume remote Git hosting, cloud deployment, or CI services are available.
- Define rollback before promoting a high-impact packaging change.

## Return Contract

Return:

- affected build and release boundary;
- toolchain, dependency, and artifact changes;
- commands and validations performed;
- install, upgrade, recovery, and rollback evidence;
- environment-only checks and residual release risk.

## Authority Boundary

This skill does not grant release approval, signing authority, branch ownership, model selection, or tool permissions.
