---
name: engineer-cpp-systems
description: Implement and review C++ systems or desktop code involving ownership, lifetime, concurrency, native integration, ABI stability, undefined behavior, and performance-sensitive paths. Use when the affected solution contains C or C++ components.
---

# Engineer C++ Systems

Make the smallest safe C++ change while preserving ownership, concurrency, binary, and build contracts.

## Procedure

1. Identify the target standard, compiler, architecture, build flags, and public binary boundary.
2. Trace object ownership, lifetime, allocation, cleanup, and cross-thread access.
3. Check error propagation, exception guarantees, and resource acquisition paths.
4. Implement the narrowest coherent change consistent with project conventions.
5. Build with the intended toolchain and inspect new diagnostics.
6. Validate normal, failure, concurrency, and native integration paths as applicable.

## Technical Focus

- RAII and deterministic cleanup;
- dangling references, bounds, alignment, and other undefined behavior;
- locks, atomics, memory ordering, and thread affinity;
- ABI and public-header compatibility;
- allocation, copying, cache behavior, and measured performance;
- platform APIs, COM, drivers, devices, and native library boundaries.

## Quality Rules

- Do not introduce speculative micro-optimization.
- Do not broaden modernization beyond the scoped task.
- Document every `unsafe` native assumption in code or handoff evidence.
- Benchmark performance claims when the environment permits.
- Preserve compiler and target-version compatibility unless explicitly changed.

## Authority Boundary

This skill does not grant file ownership, architecture approval, model selection, tool permissions, or permission to change public contracts.
