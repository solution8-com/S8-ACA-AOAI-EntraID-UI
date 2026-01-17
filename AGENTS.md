# Repository Guidelines

## Project Structure & Module Organization
- `src/quartapp/` holds the Quart backend and app logic (`chat.py`, templates, and static assets).
- `src/quartapp/templates/` and `src/quartapp/static/` contain the HTML/JS frontend.
- `tests/` contains unit tests, fixtures, and snapshots (`tests/snapshots/`).
- `infra/` and `azure.yaml` define Azure infrastructure and deployment.
- `scripts/` includes helper scripts for Entra/Azure setup.
- `docs/` and `next-steps.md` provide operational guidance.

## Build, Test, and Development Commands
- `pip install -r requirements-dev.txt` installs dev dependencies (lint, test).
- `python3 -m pip install -e src` installs the app in editable mode.
- `python -m quart --app src.quartapp run --port 50505 --reload` runs the local dev server.
- `./scripts/dev_local.sh` starts/stops Redis automatically for personal local machines (OrbStack/Docker).
- `./scripts/dev_local_github.sh` starts/stops Redis automatically for GitHub-hosted agents.
- `pytest` runs unit tests with coverage (`-ra --cov` via `pyproject.toml`).
- `pytest tests/test_n8n_integration.py` runs the env-gated n8n integration test.
- `azd up` provisions and deploys the full Azure stack; `azd deploy` updates app code.

## Coding Style & Naming Conventions
- Python 3.11 targets with 120-character lines (`pyproject.toml`).
- Format with Black: `black .`
- Lint and import ordering with Ruff: `ruff check .`
- Package imports treat `quartapp` as first-party (`known-first-party = ["quartapp"]`).

## Testing Guidelines
- Framework: `pytest` with coverage enabled and missing lines shown.
- Keep tests in `tests/` and name files `test_*.py`.
- Snapshot tests live under `tests/snapshots/`.
- For n8n integration, set `CHAT_PROVIDER=n8n`, `N8N_WEBHOOK_URL`, and `N8N_BEARER_TOKEN`.

## Commit & Pull Request Guidelines
- Recent history uses conventional prefixes like `feat:`, `fix:`, `chore:`; follow this pattern when possible.
- PRs should describe user-facing changes, include deployment impact (if any), and add screenshots for UI changes.
- Link related issues or Azure work items and note any required environment variables.

## Security & Configuration Tips
- Secrets and tenant-specific values are managed through `azd` environments and Key Vault.
- Avoid committing credentials; prefer `.azure/` envs and GitHub secrets for CI/CD.
