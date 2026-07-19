---
name: engineer-rust-systems
description: Implement and review Rust systems, desktop, library, FFI, or performance-sensitive code involving ownership, lifetimes, error models, async tasks, unsafe boundaries, synchronization, and public API stability. Use when Rust owns part of the solution.
---

# Engineer Rust Systems

Use Rust's guarantees to make behavior explicit without adding unnecessary abstraction.

## Procedure

1. Identify crate boundaries, feature flags, supported toolchain, target platforms, and public APIs.
2. Trace ownership, borrowing, lifetimes, shared state, task lifecycles, and FFI boundaries.
3. Model failures explicitly with `Result`, `Option`, or domain error types.
4. Keep `unsafe` code narrow and document the invariant that makes it sound.
5. Implement the smallest coherent change and preserve API compatibility.
6. Run formatting, checks, targeted tests, and broader workspace tests as risk requires.

## Technical Focus

- ownership and lifetime correctness;
- async cancellation, task shutdown, and runtime selection;
- synchronization, deadlock, and contention risk;
- clone, allocation, copying, and measured performance;
- panic boundaries and recoverable error propagation;
- C ABI, native handles, and memory ownership across FFI.

## Quality Rules

- Do not use cloning to hide an unresolved ownership design.
- Do not add `unsafe` without a stated and verified invariant.
- Avoid premature optimization and broad crate restructuring.
- Add property, fuzz, or benchmark follow-up when residual risk justifies it.

## Authority Boundary

This skill does not grant file ownership, architecture approval, model selection, tool permissions, or permission to change public compatibility contracts.
