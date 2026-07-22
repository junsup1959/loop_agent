# Build Validation Profile

## Validation Standard

- Materialize a clean sandbox from the exact integration OID.
- Reproduce declared build, dependency-resolution, packaging, and installation paths with isolated outputs.
- Record tool versions, dependency lock hashes, environment facts, commands, artifacts, and checksums.
- Verify applicable recovery and rollback behavior.

## Decision Rule

Approve only when source integrity is clean, required artifacts are reproducible, and the verified OID equals the QA-tested integration OID.
