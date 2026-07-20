@echo off
setlocal EnableExtensions DisableDelayedExpansion

rem Run this setup only from a normal user terminal after install_mcp_dependencies.bat succeeds.
rem It creates project-local state and configuration but does not install MCP dependencies.
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "JAVA_TOOL_OPTIONS=%JAVA_TOOL_OPTIONS% -Dfile.encoding=UTF-8"

set "DRY_RUN=0"
if /I "%~1"=="--help" (
    echo Usage: %~nx0 [--dry-run]
    echo.
    echo Run after .\scripts\install_mcp_dependencies.bat, from a normal user terminal outside Codex.
    echo --dry-run validates prerequisites and prints the setup sequence without writing.
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
    echo Example: Set-Location C:\path\to\project ^& .\scripts\setup_agent_team.bat
    exit /b 1
)

echo [Agent Team] Project root: %CD%
if "%DRY_RUN%"=="1" echo [Agent Team] Dry run: mutation commands will only be printed.

where.exe python.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Python was not found on PATH.
    exit /b 1
)
rem Match the common uv tool location used by install_mcp_dependencies.bat for this process.
set "PATH=%USERPROFILE%\.local\bin;%PATH%"
where.exe serena.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: Serena was not found on PATH.
    echo Run .\scripts\install_mcp_dependencies.bat first.
    exit /b 1
)

echo.
echo [Agent Team] Initialize core control plane
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py
) else (
    python .\scripts\init_agent_team.py
    if errorlevel 1 (
        echo ERROR: Core control-plane initialization failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Verify previously installed MCP dependencies
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --check-mcp-dependencies
) else (
    python .\scripts\init_agent_team.py --check-mcp-dependencies
    if errorlevel 1 (
        echo ERROR: MCP dependency verification failed.
        echo Run .\scripts\install_mcp_dependencies.bat before setup.
        exit /b 1
    )
)

if exist ".serena\project.yml" (
    echo.
    echo [Agent Team] Index existing Serena project
    if "%DRY_RUN%"=="1" (
        echo   serena project index
    ) else (
        serena project index
        if errorlevel 1 (
            echo ERROR: Serena project indexing failed.
            exit /b 1
        )
    )
) else (
    echo.
    echo [Agent Team] Create and index Serena project
    if "%DRY_RUN%"=="1" (
        echo   serena project create --index
    ) else (
        serena project create --index
        if errorlevel 1 (
            echo ERROR: Serena project creation failed.
            exit /b 1
        )
    )
)

echo.
echo [Agent Team] Health-check Serena project
if "%DRY_RUN%"=="1" (
    echo   serena project health-check
) else (
    serena project health-check
    if errorlevel 1 (
        echo ERROR: Serena project health check failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Initialize Serena memories
if "%DRY_RUN%"=="1" (
    echo   serena memories initialize
) else (
    serena memories initialize
    if errorlevel 1 (
        echo ERROR: Serena memory initialization failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Write the Serena stdio MCP entry to project .codex\config.toml
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --configure-mcp serena
) else (
    python .\scripts\init_agent_team.py --configure-mcp serena
    if errorlevel 1 (
        echo ERROR: Serena MCP configuration failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Write the Sequential Thinking MCP entry to project .codex\config.toml
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --configure-mcp sequentialthinking
) else (
    python .\scripts\init_agent_team.py --configure-mcp sequentialthinking
    if errorlevel 1 (
        echo ERROR: Sequential Thinking MCP configuration failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Strictly check Serena MCP
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --check-mcp serena
) else (
    python .\scripts\init_agent_team.py --check-mcp serena
    if errorlevel 1 (
        echo ERROR: Serena MCP verification failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Strictly check Sequential Thinking MCP
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --check-mcp sequentialthinking
) else (
    python .\scripts\init_agent_team.py --check-mcp sequentialthinking
    if errorlevel 1 (
        echo ERROR: Sequential Thinking MCP verification failed.
        exit /b 1
    )
)

echo.
echo [Agent Team] Verify core control plane
if "%DRY_RUN%"=="1" (
    echo   python .\scripts\init_agent_team.py --check
) else (
    python .\scripts\init_agent_team.py --check
    if errorlevel 1 (
        echo ERROR: Core control-plane verification failed.
        exit /b 1
    )
)

echo.
if "%DRY_RUN%"=="1" (
    echo [Agent Team] Dry run completed. No changes were made.
) else (
    echo [Agent Team] Setup completed successfully.
    echo [Agent Team] Trust this project and restart or reload Codex before activating seats.
)
exit /b 0
