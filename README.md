# fv-data-mcp

Standalone, authenticated Finviz research server for ChatGPT and other MCP clients.
No agent framework, LLM calls, OpenAI API key, or dependency on the original application.

## What is implemented

- Twelve read-only tools: option chains, expirations, max pain, gamma exposure/estimated gamma flip, liquidity/IV/OI metrics, combined options summary, stock price, fundamentals, quarterly statements, stock/market news, article text.
- Stateless Streamable HTTP at `/mcp`; typed input/output schemas, structured errors, bounded responses, provenance and explicit stale-data warnings.
- Auth0 JWT verification and an invitation allowlist in Azure Table Storage. Authentication and membership checks happen **before any cached data is returned**.
- Redis/Upstash **source-fetch caching shared across tools and replicas**, immutable pagination snapshots, derived-result caching, distributed miss coalescing, provider quota/cooldown coordination and user quotas.
- Non-root container, scale-to-zero Azure Container Apps templates, Key Vault/managed identity, structured logs, OpenTelemetry, alerts, budget notifications, CI and manual OIDC deployment workflow.

This is a production-oriented implementation, **not a claim that the service is already deployed or certified**. Live Finviz schemas/entitlements, Auth0-to-ChatGPT OAuth, actual Upstash behavior, cloud rollout and account billing must pass the [release checklist](docs/deployment.md#release-gates). Tests use captured/synthetic data and mocked external services, not credentials from the original project's `.env`.

## Local start

Requires Python 3.12, uv, [just](https://just.systems/), and Docker with a working engine
and Compose v2. Recipes use Bash and support just 1.21 or newer.

```bash
just setup
```

Edit the new `.env`: set `FV_DEV_AUTH_ENABLED=true`, a random local-only `FV_DEV_TOKEN`, a random `FV_CURSOR_SECRET` (at least 32 characters), and your `FV_FINVIZ_API_KEY`. Generate random values using `openssl rand -hex 32`. Leave `FV_PUBLIC_URL=http://localhost:8000` and `FV_ACCESS_BACKEND=memory` for local development. Never expose this mode through a tunnel or public ingress.

```bash
just serve-local
```

`setup` installs locked dependencies and creates `.env` with owner-only permissions
only when it is missing; it never overwrites existing configuration. `serve-local`
starts Redis, waits for readiness, and binds the Python server to `127.0.0.1:8000`.
Recipes do not load `.env` into unrelated commands; the application and Compose read
their own configuration. Do not put secrets in recipe arguments.

Alternatively: `just serve-container`. MCP clients must send `Authorization: Bearer <your-local-token>`. Production uses Auth0, never this token. Public `/health/live` and `/health/ready` are minimal probes; readiness checks Redis, not provider entitlement or all cloud dependencies.

### Use Upstash instead of local Redis

**Set `FV_REDIS_URL` in `.env`. There are no separate Upstash API-key settings.**
This application uses Redis over TCP/TLS, not Upstash's HTTP REST API.

1. Open your Redis database in the Upstash Console and find its Redis connection
   details (`redis-cli`/TCP): endpoint, port, and database password. Use the Redis TLS
   connection URL if one is provided. See [Upstash's connection guide](https://upstash.com/docs/redis/overall/getstarted).
2. Replace the existing `FV_REDIS_URL=redis://localhost:6379/0` line in `.env`:

   ```dotenv
   FV_REDIS_URL=rediss://default:YOUR_DATABASE_PASSWORD@YOUR_ENDPOINT:YOUR_PORT/0
   ```

   Replace all placeholders with your database's values, including its actual port.
   Keep **`rediss://`** (TLS), and only one active `FV_REDIS_URL` assignment. When
   assembling the URL manually, percent-encode reserved characters in the password.
   Do not paste the `https://` REST URL, REST token, or account-management API key.
   `UPSTASH_REDIS_REST_URL` and `UPSTASH_REDIS_REST_TOKEN` are not read by this server.
3. Keep the other local authentication settings from the local-start instructions.
   To use Upstash without starting Docker/local Redis, run:

   ```bash
   uv run --frozen fv-data-mcp serve --host 127.0.0.1 --port 8000
   ```

`just serve-local` still starts local Redis, even if the Python server's `.env` points
at Upstash. `just serve-container` explicitly overrides the server's Redis URL to use
the Compose Redis service; changing `.env` alone does not switch that container to
Upstash. These are intentionally local-stack commands.

The full URL contains a password: never commit it, paste it into command arguments,
or share it in logs. In Azure, store it in the Key Vault secret **`redis-url`**; the
deployment maps that secret to **`FV_REDIS_URL`**. Your local `.env` is not uploaded
by `just deploy`. Use a separate development database rather than production cache
and quota state. Existing `.env` files are not updated by `just setup`; edit yours
manually using the newly documented `.env.example`.

## Common commands

Run `just` to list all commands. Run these from this repository (or its subdirectories).

| Command | Purpose |
| --- | --- |
| `just setup` | Initialize local configuration and install locked dependencies |
| `just serve-local` | Start Redis and serve Python locally |
| `just serve-container` | Build and run the local Compose stack |
| `just stop` | Stop local containers; preserve Redis data |
| `just logs` | Follow Compose logs; the local Python process logs to its own terminal |
| `just build` | Build the local Docker image `fv-data-mcp:local` |
| `just build fv-data-mcp:dev` | Build with a custom local image tag |
| `just test` | Run tests; extra pytest arguments are forwarded |
| `just lint` / `just format` | Check lint/formatting or apply formatting |
| `just check` | Lint, formatting checks, and tests with at least 80% coverage |
| `just audit` | Audit installed dependencies |
| `just secrets-push VAULT` | Upload the three required `.env` secrets to an existing Azure Key Vault |
| `just deploy` | Dispatch the existing production GitHub Actions workflow on remote `main` |
| `just deploy-status` | List recent deployment runs |

```bash
just test tests/test_finviz_requests.py -q
just check
just audit
```

## Tools

| Tool | Main arguments / behavior |
| --- | --- |
| `get_stock_quote` | `ticker`; latest five-minute bar close, not live bid/ask |
| `list_option_expirations` | `ticker`; provider-listed dates, calendar/trading DTE |
| `get_option_chain` | `ticker`, `expiration`; optional side/strike/OI filters, page size 1–250, signed cursor |
| `get_max_pain` | `ticker`, optional 1–6 `expirations`; nearest date by default |
| `get_gamma_exposure` | Same dates; optional continuous annual decimal `risk_free_rate`, `dividend_yield` |
| `get_option_metrics` | `ticker`, `expiration`; PCR, OI leaders, absolute-delta IV and liquidity |
| `get_options_summary` | Combined analysis; nearest three listed dates by default |
| `get_stock_overview` | `ticker`; company description and provider snapshot metrics |
| `get_financial_statements` | `ticker`, `kind`: income/balance/cash_flow; quarterly provider structure |
| `get_stock_news` | `ticker`, `limit` 1–50 |
| `get_market_news` | `limit` 1–50 |
| `get_article_content` | `article_id` from a news result; public HTML only |

Use ISO dates, e.g. `2027-01-15`; ticker inputs normalize case/whitespace. For another chain page, repeat **all original parameters** with `next_cursor`. Cursors expire with their source snapshot. No orders, portfolio writes, price forecasting or generated sentiment scores. ChatGPT can interpret the supplied news, rather than this service running another LLM.

## Deployment and operation

After `az login`, confirm the intended Azure account and grant your operator identity
**Key Vault Secrets Officer** on the destination vault. Then run:

```bash
chmod 600 .env
just secrets-push YOUR_VAULT_NAME
```

This uploads only `FV_FINVIZ_API_KEY`, `FV_REDIS_URL`, and `FV_CURSOR_SECRET` as
`finviz-api-key`, `redis-url`, and `cursor-secret`. A different file can be selected
with `just secrets-push YOUR_VAULT_NAME .env.production`. The recipe sources that file
as Bash, so use only your own trusted, shell-compatible file and quote literal secrets
with single quotes where appropriate. All three values must be nonempty. Use a production
`rediss://` URL, not local Redis. See [secret upload safety](docs/deployment.md#3-store-secrets-in-key-vault).

`just deploy` requires the GitHub CLI (`gh auth login`), a configured GitHub repository
remote, the workflow pushed to `main`, and the Azure resources, secrets, GitHub variables,
and protected `production` environment described in the deployment guide. Push the
intended release to `main` first: local edits and the image from `just build` are **not**
deployed. The command only queues the workflow; monitor it with `just deploy-status`.
The workflow runs CI, builds/scans the release image, and deploys its immutable digest
through the configured approval gate. Initial cloud provisioning remains a separate,
explicit operator step; `just setup` does not provision Azure resources.

- [Azure, Auth0, Upstash and ChatGPT setup](docs/deployment.md)
- [Cache architecture, trust boundaries and costs](docs/architecture.md)
- [Calculation methodology and limitations](docs/methodology.md)
- [Operations, access administration, recovery and observability](docs/operations.md)

Adapted provider conventions and option-analysis requirements from `trading-ai-assistant` at commit `cf8fce3`; only a captured option CSV fixture is copied. No original credentials or application runtime are reused. This project is not affiliated with Finviz or OpenAI. Verify provider licensing permits your intended invited-user access before sharing data.
