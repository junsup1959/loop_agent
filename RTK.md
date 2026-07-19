# RTK - Rust Token Killer (Codex CLI)

**Usage**: Token-optimized CLI proxy for shell commands.

## Rule

Always prefix shell commands with `rtk`.

Examples:

```bash
rtk git status
rtk cargo test
rtk npm run build
rtk pytest -q
```

## Meta Commands

```bash
rtk gain            # Token savings analytics
rtk gain --history  # Recent command savings history
rtk proxy <cmd>     # Run raw command without filtering
```

## Native Windows Git Bash Routing

- On native Windows, every shell command must still start with `rtk`.
- Never invoke bare `bash` on this machine. It resolves to the WSL relay, and no WSL Bash distribution is installed.
- Use Git Bash only through the absolute executable path `C:\Program Files\Git\bin\bash.exe`.
- Run a Git Bash command string with `rtk proxy "C:\Program Files\Git\bin\bash.exe" -lc "<command>"`.
- Run a project shell script by changing directory inside the Git Bash command: `rtk proxy "C:\Program Files\Git\bin\bash.exe" -lc "cd /c/path/to/project && ./scripts/task.sh <args>"`.
- Do not place PowerShell `&`, `cmd /c`, bare `bash`, a direct `.sh` path, or `rtk run` before the Git Bash executable. `rtk` must remain the first executable and `rtk proxy` must launch Git Bash directly.
- Continue to use RTK's specialized commands for Windows-native tools, for example `rtk git status`, `rtk npm test`, and `rtk pytest -q`.

Example:
```text
rtk proxy "C:\Program Files\Git\bin\bash.exe" -lc "cd /c/Users/junsu/Documents/Codex/2026-07-16/d/agent_team && ./scripts/validate-agent-team.sh"
```


## Verification

```bash
rtk --version
rtk gain
which rtk
```
