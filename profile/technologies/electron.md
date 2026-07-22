# Electron Desktop Technology Profile

## Discovery

Map ownership across main, preload, renderer, workers, native modules, packaging, update, and offline startup boundaries.

## Engineering Focus

- Define versioned IPC request, response, error, and cancellation contracts.
- Minimize the preload bridge and validate every renderer-accessible capability.
- Keep context isolation enabled unless an approved design says otherwise.
- Allowlist IPC, navigation, protocol, shell, filesystem, device, and permission surfaces.
- Trace window, application, background-task, shutdown, and native-module lifecycles.

## Validation

Verify development and packaged behavior, one normal interaction, one IPC failure or retry, offline startup, and recovery when the main process disappears.
