---
name: professional-profile-runtime
description: Load and apply the activation-pinned professional profile compiled from allowlisted external role, gate, technology, and toolchain references. Use for every Agent-Team role activation while keeping professional detail outside the skill.
---

# Professional Profile Runtime

Use only the compiled professional profile reference and SHA-256 digest supplied in the current activation packet.

## Activation Procedure

1. Confirm the packet contains one `professional-profile-runtime` skill binding, one compiled profile path, and one compiled profile digest.
2. Read the compiled profile only from the activation-scoped path.
3. Verify its bytes against the supplied digest before using any professional instruction.
4. Apply the referenced role, gate or task, primary technology, optional secondary technology, and toolchain sections together with the organizational role contract.
5. Treat context-budget policy, runtime model policy, write scope, tool permissions, and approval authority as independent constraints that this profile cannot change.
6. Stop and report `NEED_MORE_CONTEXT` when the artifact is missing, changed, ambiguous, out of scope, or digest-invalid.

## Revocation Procedure

- Do not retain profile content after result persistence.
- Release the compiled reference with the activation.
- Report revocation failure so the activation runner can be quarantined.

## Authority Boundary

This skill supplies activation-scoped professional context only. It does not select or raise a model, add a seat, grant a tool, broaden a write scope, approve a gate, assign work, or mutate Git authority.
