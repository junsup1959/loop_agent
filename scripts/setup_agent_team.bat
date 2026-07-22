@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run this setup from the Agent-Team AX source root after dependency installation.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

set "DRY_RUN=0"
if /I "%~1"=="--help" (
    echo Usage: %~nx0 [--dry-run]
    echo.
    echo Initialize and verify the Agent-Team control plane at AGENT_TEAM_AX_ROOT.
    exit /b 0
)
if /I "%~1"=="--dry-run" (
    set "DRY_RUN=1"
    shift
)
if not "%~1"=="" (
    echo ERROR: Unknown option "%~1".
    exit /b 1
)
if not exist ".\scripts\init_agent_team.py" (
    echo ERROR: Run this batch from the Agent-Team AX source root.
    exit /b 1
)
if "%AGENT_TEAM_AX_ROOT%"=="" (
    echo ERROR: Set AGENT_TEAM_AX_ROOT to an absolute directory outside this source root.
    exit /b 1
)

if "%DRY_RUN%"=="1" (
    echo python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%"
    echo python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --refresh-mcp-config
    echo python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --check-mcp serena --check-mcp sequentialthinking --json
    echo python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --check --json
    echo [Agent Team] Dry run completed. No changes were made.
    exit /b 0
)

python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%"
if errorlevel 1 exit /b 1
python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --refresh-mcp-config
if errorlevel 1 exit /b 1
python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --check-mcp serena --check-mcp sequentialthinking --json
if errorlevel 1 exit /b 1
python .\scripts\init_agent_team.py --ax-root "%AGENT_TEAM_AX_ROOT%" --check --json
if errorlevel 1 exit /b 1

echo [Agent Team] Setup completed successfully.
exit /b 0
