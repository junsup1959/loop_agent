---
name: engineer-electron-desktop
description: Implement and review Electron desktop applications across main, preload, and renderer processes, including IPC contracts, security boundaries, lifecycle, native integration, packaging, updates, and offline behavior. Use when Electron owns the desktop runtime.
---

# Engineer Electron Desktop

Treat Electron as a cross-process desktop system with explicit trust and lifecycle boundaries.

## Procedure

1. Map ownership across main, preload, renderer, workers, and native modules.
2. Define IPC request, response, error, cancellation, and versioning contracts.
3. Minimize the preload bridge and validate every renderer-accessible capability.
4. Trace window, application, background-task, and shutdown lifecycles.
5. Check filesystem, protocol, shell, device, and native module boundaries.
6. Validate development and packaged behavior, including offline startup and failure recovery.

## Security Rules

- Keep context isolation enabled unless an approved design explicitly requires otherwise.
- Do not expose Node or unrestricted IPC surfaces to renderer code.
- Validate and allowlist IPC channels and external navigation.
- Review content security policy, session, permission, and update implications when touched.

## Quality Rules

- Verify one normal interaction and one IPC failure or retry path.
- Prevent renderer dead ends when the main process rejects or disappears.
- Keep signing and update checks explicit and environment-specific.
- Do not redesign all process boundaries for a scoped defect.

## Authority Boundary

This skill does not grant security approval, signing authority, file ownership, model selection, or tool permissions.
