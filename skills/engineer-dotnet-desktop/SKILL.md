---
name: engineer-dotnet-desktop
description: Implement and review C# desktop solutions across modern .NET and .NET Framework 4.8, including WPF, WinForms, services, async flows, dependency injection, configuration, COM interop, and Windows-bound compatibility. Use for .NET desktop or mixed solution work.
---

# Engineer .NET Desktop

Preserve runtime, UI-thread, service-lifetime, and Windows integration contracts while making scoped C# changes.

## Procedure

1. Identify the exact target runtime, target framework, UI framework, architecture, and deployment model.
2. Trace the event or command through UI, service, persistence, and native boundaries.
3. Check async and cancellation flow, UI-thread affinity, exception surfaces, and state lifetime.
4. Verify nullability, dependency injection lifetimes, configuration, serialization, and public contracts.
5. Treat COM, registry, device, and native library integration as explicit compatibility boundaries.
6. Build and test with the declared target runtime rather than assuming modern .NET behavior.

## Compatibility Modes

- For .NET Framework 4.8, check binding redirects, config transforms, AppDomain behavior, legacy serialization, and framework-era package limits.
- For modern .NET, check host configuration, dependency injection, trimming or single-file assumptions, and platform-specific APIs.
- For WPF or WinForms, check dispatcher or message-loop behavior, cancellation, shutdown, and background work.

## Quality Rules

- Avoid fire-and-forget work and swallowed exceptions.
- Do not block the UI thread with synchronous I/O.
- Preserve previous-version data and installation contracts.
- Keep modernization proposals separate from scoped fixes.

## Authority Boundary

This skill does not grant file ownership, architecture approval, model selection, tool permissions, or permission to change supported runtime targets.
