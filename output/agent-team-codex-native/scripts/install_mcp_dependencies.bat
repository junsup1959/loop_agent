@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run this installer only from a normal user terminal, not from an active Codex sandbox.
rem It installs executable dependencies but intentionally does not initialize Serena user configuration.

set "DRY_RUN=0"
if /I "%~1"=="--help" (
    echo Usage: %~nx0 [--dry-run]
    echo.
    echo Run from a normal user terminal outside Codex, with the target project as the current directory.
    echo --dry-run validates prerequisites and prints the installation sequence without writing.
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
    echo ERROR: Run this batch from the target project root.
    echo Example: Set-Location C:\path\to\project ^& .\scripts\install_mcp_dependencies.bat
    exit /b 1
)

echo [Agent Team] Project root: %CD%
if "%DRY_RUN%"=="1" echo [Agent Team] Dry run: mutation commands will only be printed.

where.exe python.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    exit /b 1
)
where.exe npm.cmd >nul 2>&1
if errorlevel 1 (
    echo ERROR: Node.js npm was not found on PATH.
    exit /b 1
)

rem uv normally exposes installed tools here on Windows. This changes only this process PATH.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where.exe serena.exe >nul 2>&1
if errorlevel 1 (
    where.exe uv.exe >nul 2>&1
    if errorlevel 1 (
        echo ERROR: Serena is not on PATH and uv.exe is unavailable.
        echo Install uv in this normal user terminal, then rerun this batch.
        exit /b 1
    )
    echo.
    echo [Agent Team] Install Serena CLI
    if "%DRY_RUN%"=="1" (
        echo   uv tool install -p 3.13 serena-agent
    ) else (
        uv tool install -p 3.13 serena-agent
        if errorlevel 1 (
            echo ERROR: Serena CLI installation failed.
            exit /b 1
        )
        where.exe serena.exe >nul 2>&1
        if errorlevel 1 (
            echo ERROR: Serena was installed but is not visible on PATH.
            echo Open a new normal terminal and rerun this batch.
            exit /b 1
        )
    )
)

echo.
echo [Agent Team] Create or update project .gitignore and install global Sequential Thinking dependency
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --install-mcp-dependencies
) else (
    python .\scripts\init_agent_team.py --install-mcp-dependencies
    if errorlevel 1 (
        echo ERROR: Sequential Thinking dependency installation failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Verify MCP dependencies without configuring Codex
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --check-mcp-dependencies
) else (
    python .\scripts\init_agent_team.py --check-mcp-dependencies
    if errorlevel 1 (
        echo ERROR: MCP dependency verification failed.
        exit /b 1
    )
)

echo.
if "%DRY_RUN%"=="1" (
    echo [Agent Team] Dry run completed. No changes were made.
) else (
    echo [Agent Team] MCP dependencies installed successfully.
    echo [Agent Team] Next run: .\scripts\setup_agent_team.bat
)
exit /b 0
