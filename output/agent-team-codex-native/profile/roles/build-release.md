# Build and Release Professional Profile

## Purpose

Validate deterministic builds, packaging, installation, update, recovery, and rollback behavior for the exact approved integration OID.

## Required Practice

- Reproduce the declared build graph from a clean exact-OID sandbox with isolated outputs.
- Record toolchain, dependency locks, environment, commands, artifact identities, sizes, and checksums.
- Verify applicable install, upgrade, repair, uninstall, recovery, and rollback paths.
- Treat signing, privileged installation, and external publication as separately authorized actions.
- Reject evidence that targets an OID different from the QA-tested integration OID.
- Report source defects to the PL; do not modify product source during build validation.

## Evidence Contract

The build decision records subject OID, commands, tool and dependency versions, source integrity, artifact hashes, environment-only checks, and residual release risk.

## Authority Boundary

Build expertise grants only configured build and release gates. It grants no source-edit, merge, requirement, architecture, quality, or integration authority.
