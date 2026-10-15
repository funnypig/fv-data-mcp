set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments := true
set dotenv-load := false

# List available commands.
default:
    @just --list

# Create local configuration if missing and install locked Python dependencies.
setup: env-init
    uv sync --frozen
    @printf '%s\n' 'Edit .env with your local credentials, then run: just serve-local'

# Create a private .env from the example without overwriting existing configuration.
env-init:
    @if [[ -e .env || -L .env ]]; then printf '%s\n' 'Keeping existing .env'; else (umask 077; set -C; cat .env.example > .env); printf '%s\n' 'Created .env; configure it before serving.'; fi

# Start local Redis and wait for its health check.
redis-up:
    docker compose up -d --wait redis

# Run the Python server on loopback; Redis starts automatically.
serve-local: redis-up
    uv run --frozen fv-data-mcp serve --host 127.0.0.1 --port 8000

# Build and run the server and Redis through Compose (foreground logs).
serve-container:
    docker compose --profile server up --build

# Stop local containers without deleting the Redis data volume.
stop:
    docker compose --profile server down

# Follow local container logs.
logs:
    docker compose --profile server logs --follow

# Build a local container image, optionally specifying its tag.
build image="fv-data-mcp:local":
    docker build --tag "$1" .

# Check Python lint and formatting without modifying files.
lint:
    uv run --frozen ruff check .
    uv run --frozen ruff format --check .

# Format Python source and tests.
format:
    uv run --frozen ruff format .

# Run tests; pass additional pytest arguments after the recipe name.
test *args:
    uv run --frozen pytest "$@"

# Run lint, formatting checks, and tests with the CI coverage threshold.
check: lint
    uv run --frozen pytest --cov=fv_data_mcp --cov-fail-under=80

# Audit the installed project environment for known dependency vulnerabilities.
audit:
    uv run --frozen pip-audit --local

# Upload only the three required .env secrets to an explicitly named Azure Key Vault.
secrets-push vault env_file=".env":
    #!/usr/bin/env bash
    set +xv
    set -euo pipefail
    source "$2"
    : "${FV_FINVIZ_API_KEY:?Set FV_FINVIZ_API_KEY}" "${FV_REDIS_URL:?Set FV_REDIS_URL}" "${FV_CURSOR_SECRET:?Set FV_CURSOR_SECRET}"
    export AZURE_LOGGING_ENABLE_LOG_FILE=false AZURE_CORE_COLLECT_TELEMETRY=false
    printf '%s' "$FV_FINVIZ_API_KEY" | az keyvault secret set --vault-name "$1" --name finviz-api-key --file /dev/stdin --encoding utf-8 --output none --only-show-errors
    printf '%s' "$FV_REDIS_URL" | az keyvault secret set --vault-name "$1" --name redis-url --file /dev/stdin --encoding utf-8 --output none --only-show-errors
    printf '%s' "$FV_CURSOR_SECRET" | az keyvault secret set --vault-name "$1" --name cursor-secret --file /dev/stdin --encoding utf-8 --output none --only-show-errors

# Dispatch the existing production workflow for remote main, not uncommitted local code.
deploy:
    @printf '%s\n' 'Dispatching remote main through GitHub CI and production approval gates.'
    gh workflow run deploy.yaml --ref main

# List recent production deployment workflow runs.
deploy-status:
    gh run list --workflow deploy.yaml --limit 5
