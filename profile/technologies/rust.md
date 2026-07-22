# Rust Technology Profile

## Discovery

Confirm crate boundaries, workspace manifests, feature flags, supported toolchain, target platforms, public APIs, and FFI surfaces.

## Engineering Focus

- Make ownership, borrowing, lifetimes, shared state, and task shutdown explicit.
- Model recoverable failures with typed `Result` and `Option` contracts.
- Keep unsafe code narrow and state the invariant that makes it sound.
- Examine synchronization, deadlock, contention, allocation, copying, and panic boundaries.
- Preserve public API and C ABI compatibility unless a versioned change is authorized.

## Validation

Run formatting, checks, targeted tests, relevant workspace tests, and justified property, fuzz, or benchmark checks.
