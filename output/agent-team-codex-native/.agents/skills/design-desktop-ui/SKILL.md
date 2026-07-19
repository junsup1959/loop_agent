---
name: design-desktop-ui
description: Design and review implementation-ready desktop UI behavior, event flow, information hierarchy, keyboard access, background work, error recovery, high-DPI behavior, and platform consistency. Use for WPF, WinForms, native Windows, Electron, or PowerShell desktop interfaces.
---

# Design Desktop UI

Specify desktop interactions precisely enough for implementation and test without prescribing an unnecessary redesign.

## Procedure

1. Identify the user goal, primary workflow, platform conventions, and existing visual language.
2. Define layout hierarchy, component ownership, state transitions, and event flow.
3. Specify loading, empty, error, disabled, cancellation, and recovery states.
4. Define background-work behavior so long operations do not freeze the UI.
5. Specify keyboard order, shortcuts, focus restoration, screen-reader labels, scaling, and localization constraints.
6. Map each interaction requirement to an observable acceptance test.

## Quality Rules

- Separate UI state from automation or domain logic.
- Provide immediate and recoverable feedback for user actions.
- Preserve expected desktop behavior for resize, close, minimize, shutdown, and multiple windows.
- Account for high DPI, font scaling, long text, and target input devices.
- Prefer existing components and tokens over new abstractions.

## Return Contract

Return:

- screen or component scope;
- layout and interaction specification;
- state and event transition table;
- accessibility and platform requirements;
- implementation constraints;
- unresolved product decisions.

## Authority Boundary

This skill supplies design expertise only. It does not grant product approval, file ownership, model selection, tool permissions, or permission to replace the existing design system.
