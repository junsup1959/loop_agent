# Architecture Review Profile

## Review Standard

- Map affected behavior, dependencies, interface direction, state ownership, lifecycle, concurrency, and failure isolation.
- Check API, ABI, data-format, installation, and previous-version compatibility.
- Compare viable alternatives by material risk, complexity, migration cost, and reversibility.
- Preserve the existing architecture unless the approved problem requires the reviewed boundary change.

## Decision Rule

Approve only when declared constraints and compatibility requirements are satisfied by exact-OID source and executable evidence. Record unresolved risks and required downstream validation.
