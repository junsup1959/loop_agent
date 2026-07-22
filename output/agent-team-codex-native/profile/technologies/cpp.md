# C and C++ Technology Profile

## Discovery

Confirm language standard, compiler, target architecture, build flags, runtime library, public headers, native dependencies, and ABI boundary.

## Engineering Focus

- Trace ownership, lifetime, allocation, cleanup, and cross-thread access.
- Use deterministic resource management and explicit exception guarantees.
- Check bounds, alignment, dangling access, races, memory ordering, and other undefined behavior.
- Preserve ABI, public-header, compiler, and target-version compatibility.
- Measure performance claims and avoid speculative micro-optimization.

## Validation

Build with the intended toolchain and exercise normal, failure, concurrency, and native-integration paths affected by the change.
