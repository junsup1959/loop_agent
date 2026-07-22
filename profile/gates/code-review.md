# Code Quality Review Profile

## Review Standard

- Review the exact candidate OID independently from the implementation activation.
- Trace correctness, invariants, errors, state mutation, concurrency, side effects, compatibility, and regression coverage.
- Execute focused checks when static inspection alone is insufficient.
- Treat exploratory source changes as analysis only and require a clean rerun for approval.

## Finding Contract

Each finding includes severity, blocking status, precise location, evidence, failure scenario, user or system impact, smallest remediation, and missing validation.

## Decision Rule

Approve only when no blocking correctness or regression finding remains and source integrity is clean.
