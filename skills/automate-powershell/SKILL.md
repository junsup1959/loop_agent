---
name: automate-powershell
description: Implement and review Windows PowerShell 5.1 or PowerShell 7 automation, modules, administration tools, and interactive scripts with explicit version, encoding, error, privilege, idempotency, and rollback behavior. Use for Windows automation or PowerShell-based tooling.
---

# Automate PowerShell

Write reliable automation against the declared PowerShell edition and target host.

## Procedure

1. Declare Windows PowerShell 5.1 or PowerShell 7 before choosing syntax or modules.
2. Identify target hosts, required modules, .NET runtime, elevation needs, and execution policy constraints.
3. Define parameter contracts, pipeline behavior, output objects, exit codes, and error semantics.
4. Make state-changing operations idempotent and add confirmation or rollback for high-impact actions.
5. Check path, encoding, locale, and quoting behavior on the target Windows environment.
6. Validate normal, invalid-input, partial-failure, and repeated-run behavior.

## Version Rules

- Do not assume PowerShell 7 behavior in Windows PowerShell 5.1.
- In 5.1, account for full .NET Framework, legacy module loading, and encoding defaults.
- In 7, account for cross-platform behavior only when the task targets non-Windows hosts.

## Quality Rules

- Distinguish terminating and non-terminating errors.
- Never expose credentials, tokens, or secret values in logs.
- Mark commands that require elevation or a specific host.
- Do not launch visible windows unless the user needs interactive control.

## Authority Boundary

This skill does not grant administrative authority, elevation, file ownership, model selection, or tool permissions.
