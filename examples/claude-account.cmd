@echo off
rem Generic per-account launcher for claude-accounts.
rem Usage: claude-account.cmd 00 [claude args...]
setlocal
if "%~1"=="" (
  echo usage: claude-account.cmd ^<slug^> [claude args...]
  exit /b 2
)
set "SLUG=%~1"
shift

set "BASE=%USERPROFILE%\.config\claude-subscriptions"
set "CLAUDE_CONFIG_DIR=%BASE%\configs\%SLUG%"

rem Never let an API key or a third-party endpoint win over the subscription.
for %%V in (ANTHROPIC_API_KEY ANTHROPIC_BASE_URL ANTHROPIC_AUTH_TOKEN
            CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX) do set "%%V="

rem claude-accounts stores tokens in tokens.json; login once with:
rem   claude-accounts add
rem then let claude-accounts launch this slug instead of re-implementing auth here.
call claude-accounts launch %SLUG% %*
exit /b %errorlevel%
