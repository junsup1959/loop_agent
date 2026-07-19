# RTK - Rust Token Killer

## Rule

Every native shell command must start with `rtk`.

Examples:

```text
rtk git status
rtk cargo test
rtk npm run build
rtk pytest -q
```

## Complex Commands

For a compound command, shell built-in, or a command that cannot be safely prefixed directly, keep `rtk` as the first executable and use the proxy form:

```text
rtk proxy <executable> <arguments>
```

On native Windows, do not invoke bare `bash`. Use the installed Git Bash executable through `rtk proxy` when a POSIX shell command is required.

## Enforcement

The project-local Codex `PreToolUse` hook reads this policy before allowing a shell command. It rewrites supported simple native commands with `rtk` and rejects complex or unsupported commands until they are submitted in an explicit `rtk` form.
