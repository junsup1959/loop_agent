---
name: engineer-embedded-devices
description: Implement and review embedded, firmware, driver, or device-integrated software involving timing, constrained resources, interrupts, protocols, state machines, connectivity, diagnostics, firmware compatibility, and recovery. Use when the offline solution communicates with or runs on hardware.
---

# Engineer Embedded Devices

Protect deterministic behavior and recovery at hardware-software boundaries.

## Procedure

1. Identify target hardware, firmware version, CPU, memory, power, timing, and protocol constraints.
2. Map startup, steady-state, shutdown, fault, reset, and update state transitions.
3. Trace drivers, buses, interrupts, buffers, commands, acknowledgements, and ownership boundaries.
4. Check concurrency, ordering, timeouts, retries, duplication, and noisy-input behavior.
5. Keep changes compatible with deployed firmware and host software or define an explicit migration.
6. Validate in simulation where useful and list required bench or device tests.

## Quality Rules

- Do not infer real-time safety from desktop-only tests.
- Keep interrupt work bounded and shared-state rules explicit.
- Define watchdog, disconnect, reconnect, reset, and partial-update behavior.
- Make command handling idempotent where retransmission is possible.
- Preserve diagnostic visibility under constrained telemetry.
- Stage firmware or fleet changes and define rollback.

## Return Contract

Return:

- hardware and protocol boundary;
- timing, state, and resource constraints;
- change or diagnosis with supporting evidence;
- host, simulation, and device validation;
- compatibility, update, and recovery risks.

## Authority Boundary

This skill does not grant device access, firmware release approval, hardware safety certification, file ownership, model selection, or tool permissions.
