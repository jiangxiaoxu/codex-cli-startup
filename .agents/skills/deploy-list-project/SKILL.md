---
name: deploy-list-project
description: Build and deploy the standalone list-project Windows console executable and its PowerShell navigation wrapper. Use when Codex needs to publish or refresh list-project.exe and list-project.ps1 below the current user's LOCALAPPDATA codex-cli-startup directory, verify the deployed artifacts, or reproduce the local CLI deployment.
---

# Deploy List Project

Build the current repository source and deploy only the CLI artifacts. Resolve the destination from `LOCALAPPDATA`; never hardcode a user profile path.

## Workflow

1. Run `python setup_env.py --build` from the repository root.
2. Run `.\.venv\Scripts\python.exe .agents\skills\deploy-list-project\scripts\deploy.py` from the repository root.
3. Confirm the script reports both deployed paths and a successful executable smoke test.
4. Run `Invoke-ScriptAnalyzer -Path "$env:LOCALAPPDATA\codex-cli-startup\list-project.ps1" -Severity Warning,Error`.
5. Report the resolved destination and validation results. Do not modify or remove `codex-cli-startup_config.json`.

The deployment script rebuilds `dist/list-project.exe`, atomically replaces the deployed exe and ps1, verifies their SHA-256 hashes, and runs `list-project.exe --help` without reading or changing workspace configuration.
