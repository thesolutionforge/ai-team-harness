@echo off
rem Signs Codex (GPT) in to YOUR ChatGPT account, for this harness only.
rem The login is stored in the .codex-home folder next to this file, separate
rem from the Codex desktop app's own settings.
cd /d "%~dp0"
python -c "import conductor; conductor.ensure_codex_home()"
set "CODEX_HOME=%~dp0.codex-home"
echo A browser window will open. Sign in with the ChatGPT account whose plan you want to use.
codex login
echo.
codex login status
pause