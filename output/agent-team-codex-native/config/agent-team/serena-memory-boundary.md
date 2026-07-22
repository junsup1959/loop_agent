# Serena Project-Memory Boundary

This policy governs initial onboarding and every shared Serena memory refresh.

The PL capability alone may publish or refresh shared memory. Before issuing a
developer implementation or rework handoff, PL records a source-OID- and
policy-digest-pinned onboarding snapshot and an `initial_instructions` usage
receipt. The developer reads only the named bindings in that contract and records
their consumption receipts before the first source mutation.

## Allowed Content

Publish only slow-changing, project-specific knowledge that is supported by the target repository or an explicitly approved project source. The `conventions` memory may contain coding style, naming, directory layout, build, test, lint, formatting, review, and contribution conventions of the target project.

Stable module structure and ownership guidance are allowed. Rapidly changing code
summaries belong in activation artifacts, not shared memory.

## Prohibited Content

Never publish agent-team operating rules, role or seat definitions, agent-to-agent message contracts, goal/module/research-loop workflows, Plan IR or TaskFlow procedures, context-packet rules, tool-selection instructions, prompt text, sandbox or approval policy, or any content whose purpose is to control agents rather than describe the target project.

Do not publish active task or work-item state, current diffs, OID approvals,
workspace leases, team roles, workflow state, activation contracts, prompt text,
test runs, chat history, SQLite messages, or per-run results and evidence. Keep
those records in the agent-team control plane and task artifacts.

## Publication Check

Before the PL publishes or refreshes a shared memory, verify that every proposed statement is project knowledge, traceable to an allowed source, and independent of the agent-team implementation. Exclude the statement when any check fails.

Bind only the minimum transition-specific names, references, and SHA-256 digests.
Wildcard, all-memory, `docs/`, duplicate, missing, changed, or digest-invalid
bindings fail closed.

For developer implementation and rework, every selected named binding must be
read and its exact snapshot/name/digest receipt persisted before the first source
mutation. The result may only replay those same contract-bound receipts.
