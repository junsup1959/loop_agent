---
name: engineer-test-coverage
description: Design and implement risk-based automated test coverage, fixtures, harnesses, regression checks, and release gates. Use when changed behavior needs executable confidence across normal, failure, boundary, integration, recovery, or performance paths.
---

# Engineer Test Coverage

Map material risks to deterministic tests and explicit release evidence.

## Procedure

1. Read acceptance criteria, the changed behavior map, and known risks.
2. Build a risk-to-test matrix before adding cases.
3. Select the smallest suitable test level: unit, component, integration, system, or manual environment check.
4. Create deterministic fixtures and isolate mutable external state.
5. Verify the test fails for the defect or missing behavior when practical.
6. Run the narrow suite, then the relevant regression boundary.
7. Record coverage gaps and environment-only checks.

## Required Coverage

For material changes, include:

- primary success behavior;
- representative failure and recovery behavior;
- boundary or invalid input;
- one affected integration edge;
- concurrency, performance, security, or compatibility checks when relevant.

## Quality Rules

- Assert behavior contracts rather than implementation details.
- Avoid timing-dependent sleeps and shared mutable fixtures.
- Balance confidence against suite runtime and maintenance cost.
- Define go/no-go evidence for high-risk changes.
- Do not demand exhaustive testing for low-risk scoped work.

## Authority Boundary

This skill may guide or implement tests only when the task grants write ownership. It does not grant release approval, model selection, or tool permissions.
