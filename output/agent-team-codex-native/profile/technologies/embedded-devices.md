# Embedded and Device Technology Profile

## Discovery

Identify hardware, firmware, CPU, memory, power, timing, protocol, driver, bus, interrupt, and deployed compatibility constraints.

## Engineering Focus

- Model startup, steady-state, shutdown, fault, reset, reconnect, and update transitions.
- Keep interrupt work bounded and shared-state ownership explicit.
- Check ordering, timeouts, retries, duplication, buffer limits, and noisy input.
- Make retransmitted commands idempotent where possible.
- Preserve diagnostic visibility and define watchdog, partial-update, and rollback behavior.

## Validation

Separate host, simulation, bench, and device evidence. Do not infer real-time or hardware safety from desktop-only tests.
