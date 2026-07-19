---
name: debug-runtime-failures
description: Isolate software failures using reproduction evidence, logs, stack traces, state and control-flow analysis, targeted instrumentation, and hypothesis testing. Use for crashes, hangs, incorrect behavior, flaky tests, timing faults, or environment-specific failures.
---

# Debug Runtime Failures

Find the smallest evidence-supported root cause and avoid symptom-masking fixes.

## Procedure

1. Record the trigger, expected behavior, observed behavior, environment, and reproducibility.
2. Reduce the failure to the smallest reliable case.
3. Trace control flow, data flow, state transitions, and external boundaries.
4. Rank hypotheses with supporting and disconfirming evidence.
5. Add narrow diagnostics or tests when existing evidence cannot distinguish hypotheses.
6. Confirm the root cause before proposing or applying a fix.
7. Validate the corrected path, the original failure path, and one integration edge.

## Quality Rules

- Distinguish observed fact, inference, and untested hypothesis.
- Consider concurrency, ordering, configuration, encoding, permissions, and dependency versions.
- Do not claim a definitive root cause without direct evidence.
- Prefer a causal fix over retries, suppression, or broad exception handling.
- Preserve diagnostic evidence needed for review.

## Return Contract

Return:

- reproduction conditions and failure signature;
- evidence timeline and affected path;
- ranked hypotheses;
- confirmed root cause or next discriminating check;
- minimal fix recommendation when requested;
- validations performed and residual uncertainty.

## Authority Boundary

This skill does not grant permission to edit code or production state. Diagnosis and implementation remain separate task permissions.
