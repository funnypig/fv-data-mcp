# Deployment: Azure + Auth0 + Upstash + ChatGPT

No subscription, tenant or cache is assumed to exist. Provisioning is intentionally a separate operator action: these instructions incur charges and must be reviewed. Use a private repository unless the reused fixture and provider terms permit publication.

## 1. Accounts and foundation

Create an Azure subscription, Auth0 tenant, and Upstash Redis database near West Europe. Use a TLS Redis endpoint (`rediss://...`), a single primary region, persistence, and no eviction of quota/lock keys. Confirm Lua, `TIME`, sorted sets, conditional `SET` and required command/value limits on your plan. This implementation uses the Redis TCP interface, not the REST API. Do not configure a read replica endpoint. Auth0 and Upstash are external resources, not created by Bicep.

**Where to configure Upstash:** use the database's Redis/TCP connection details
(endpoint, port, database password) to obtain a URL in this form:
`rediss://default:YOUR_DATABASE_PASSWORD@YOUR_ENDPOINT:YOUR_PORT/0`.
Prefer a provided Redis connection URL; otherwise percent-encode reserved characters
in the password when assembling it. The local setting is **`FV_REDIS_URL`** in `.env`.
For Azure, put the full URL in the **`redis-url`** Key Vault secret in step 3; the app
template exposes it as `FV_REDIS_URL`. Do not use the REST URL/token or an Upstash
account-management API key. No separate `UPSTASH_*` variables are consumed. See the
[Upstash connection guide](https://upstash.com/docs/redis/overall/getstarted) and the
[local Upstash instructions](../README.md#use-upstash-instead-of-local-redis).

Install Azure CLI with Bicep, uv/Python 3.12 and Docker. Log in as an operator permitted to create resources and role assignments. Choose a 3–10 character lowercase alphanumeric prefix starting with a letter.

```bash
az login
az account set --subscription YOUR_SUBSCRIPTION_ID
az group create -n fv-data-mcp-prod -l westeurope
az deployment group what-if -g fv-data-mcp-prod -f infra/foundation.bicep \
  -p prefix=fvdata operationsEmail=YOU@example.com
az deployment group create -g fv-data-mcp-prod -f infra/foundation.bicep \
  -p prefix=fvdata operationsEmail=YOU@example.com --name foundation
az deployment group show -g fv-data-mcp-prod -n foundation --query properties.outputs
```

Review defaults: Consumption min=0/max=2 in app template, ACR Basic, LRS storage, 30-day logs, daily log cap 0.1 GB, vault soft delete/purge protection, 50-unit budget notification. Public Azure service endpoints are authenticated with managed identity; private networking/WAF is not part of this low-cost deployment. Budget alerts are not spending caps. Log caps can suppress evidence/alerts when reached. Resource-provider registration and RBAC propagation may take time; rerun a failed operation after confirming its cause rather than changing access to anonymous.

Record outputs: `publicUrl`, `registryName`, `registryServer`, `vaultName`, `storageName`, `tableEndpoint`, `blobEndpoint`. Keep `publicUrl` stable: it determines the OAuth audience.

## 2. Configure Auth0

Use an HTTPS tenant origin for `FV_AUTH0_ISSUER` / the `auth0Issuer` deployment parameter.
Both `https://YOUR-TENANT.eu.auth0.com` and `https://YOUR-TENANT.eu.auth0.com/` work:
the application adds a missing trailing slash before token verification, discovery,
and membership lookups (including CLI grants). Token issuer validation remains exact;
paths, credentials, query strings and fragments are not accepted in the configured origin.

1. Create an Auth0 API with identifier **exactly** `<publicUrl>/mcp`, RS256 signing, and permission `research:read`. Configure short access-token lifetime (e.g. one hour).
2. Enable the **Resource Parameter Compatibility Profile** for MCP resource indicators. Clients send `resource`; the resulting token must have the MCP API audience, not only `/userinfo`. Check Auth0 authorization-server discovery supports PKCE S256 and issuer identification expected by the client.
3. Pre-register a Regular Web Application for the ChatGPT connection (authorization-code flow with PKCE). Use the exact callback URL shown by your ChatGPT connection UI/current documentation; do not guess or wildcard it. Configure the supported token-endpoint authentication method consistently with the client credentials entered in ChatGPT. Refresh tokens/offline access are optional; if enabled, configure rotation and reuse detection.
4. Restrict signup/invite access in Auth0 and use MFA for the owner/operator. Turn off unneeded connections/grants. Dynamic client registration is **not needed for the pre-registered-client path**; do not enable open DCR just to bypass a configuration mismatch. If your ChatGPT client cannot accept pre-registered credentials, validate its supported registration flow before deployment.
5. Invite/create each Auth0 user. After the user exists, use their exact `user_id`/JWT `sub` to grant local membership. Auth0 signup by itself never grants research access.

Auth0 behavior and the ChatGPT settings UI can change. Validate the authorization dance with a test user before sharing. References: [OpenAI authentication requirements](https://developers.openai.com/plugins/build/auth), [Auth0 MCP compatibility settings](https://auth0.com/blog/secure-csharp-mcp-server-with-auth0/).

## 3. Store secrets in Key Vault

Assign the operator **Key Vault Secrets Officer** at vault scope. Runtime already has
**Key Vault Secrets User**. Wait for RBAC propagation. To upload directly from your
local `.env` on Linux/macOS/WSL:

```bash
az login
az account show --query '{subscription:name, tenant:tenantId}' --output table
chmod 600 .env
just secrets-push YOUR_VAULT_NAME
```

Replace `YOUR_VAULT_NAME` with the foundation's `vaultName` output. Authenticate to
the intended account first; the command does not create a vault or change permissions.
For a separate deployment file: `just secrets-push YOUR_VAULT_NAME .env.production`
(restrict that file's permissions too).

| `.env` setting | Key Vault secret |
| --- | --- |
| `FV_FINVIZ_API_KEY` | `finviz-api-key` |
| `FV_REDIS_URL` | `redis-url` |
| `FV_CURSOR_SECRET` | `cursor-secret` |

The recipe **sources the file as Bash**, which executes its contents. Use only your
own trusted, shell-compatible file; single-quote literal values containing `$`, spaces,
or other shell syntax. Settings in the file override exported variables; values absent
from the file can come from your shell. All three values must be nonempty before any
upload starts. No other validation is performed: use real credentials, a production
`rediss://` Redis URL, and a random cursor secret of at least 32 characters.

The three CLI commands receive values through stdin, not command arguments or temporary
secret files. Shell tracing, Azure CLI file logging, and CLI telemetry are disabled;
Successful CLI responses are suppressed; errors remain visible for troubleshooting.
Do not enable tracing inside `.env` or add debug flags. Review and redact diagnostics
before sharing them. If a command fails, check login, vault permissions, and connectivity.
Keep `.env` private and git-ignored: it remains plaintext
and is not protected from your account, administrators, or malware.

Uploads stop on the first failure, but earlier writes remain. Successful writes create
new secret versions; rerunning is not an atomic rollback. The recipe does not add retries,
deploy, or restart the app. After rotation, refresh/restart existing revisions as described
in the operations runbook.

Alternatively, create secrets using separate restricted-permission files or the portal:

```bash
az keyvault secret set --vault-name VAULT --name finviz-api-key --file /secure/path/finviz-key --output none
az keyvault secret set --vault-name VAULT --name redis-url --file /secure/path/redis-url --output none
az keyvault secret set --vault-name VAULT --name cursor-secret --file /secure/path/cursor-secret --output none
```

Files should contain only the value (no trailing newline). Cursor secret: at least 32 random characters. Never pass secrets as literal CLI arguments, Bicep parameters, commit them, print them in logs, or reuse the reference repository's entire `.env`. Grant only permissions needed by each operator. The application has **no** Auth0 client secret; that credential belongs to the OAuth client configuration in ChatGPT.

## 4. Build and deploy

```bash
uv sync --frozen
uv run pytest
uv run ruff check .
uv run pip-audit --local
REGISTRY=YOUR_REGISTRY_NAME
RELEASE_ID=$(git rev-parse HEAD)
SERVER=$(az acr show --name "$REGISTRY" --query loginServer -o tsv)
az acr login --name "$REGISTRY"
docker build --platform linux/amd64 --tag "$SERVER/fv-data-mcp:$RELEASE_ID" .
docker push "$SERVER/fv-data-mcp:$RELEASE_ID"
DIGEST=$(az acr repository show --name "$REGISTRY" --image "fv-data-mcp:$RELEASE_ID" --query digest -o tsv)
[[ "$DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]]
az deployment group what-if -g fv-data-mcp-prod -f infra/app.bicep \
  -p prefix=fvdata imageDigest="$DIGEST" auth0Issuer=https://YOUR-TENANT.eu.auth0.com/
az deployment group create -g fv-data-mcp-prod -f infra/app.bicep \
  -p prefix=fvdata imageDigest="$DIGEST" auth0Issuer=https://YOUR-TENANT.eu.auth0.com/ --name release-1
```

Always deploy an immutable registry digest, not `latest`. Templates use a user-assigned identity for ACR image pull, Key Vault references and Table reads. No market data is fetched at startup. The initial request may cold-start Python/scientific dependencies; assess client timeout behavior before deciding whether to pay for `minReplicas=1`.

Builds run on your local Docker engine or the GitHub Actions runner, not ACR Tasks.
If `az acr build` fails with `TasksOperationsNotAllowed`, use the Docker build/push
steps above: they do not call the restricted Tasks service. This still requires a
working Docker engine and registry push/pull permissions for the operator or deployment
identity (for example, registry-scoped `AcrPush` in RBAC-only mode). If you specifically
need Azure-hosted ACR Tasks, request access through Azure support instead. See
[Microsoft's Docker push/pull guide](https://learn.microsoft.com/en-us/azure/container-registry/container-registry-get-started-docker-cli).

Smoke-check HTTPS discovery and health. Anonymous POST `/mcp` must return 401 with the `WWW-Authenticate` resource metadata link. Readiness probes use Redis but are not an end-to-end authenticated acceptance test.

For GitHub deployment, configure a protected `production` environment with reviewers and main-branch-only deployment. Create an Entra workload identity federated to `repo:OWNER/REPO:environment:production`, audience `api://AzureADTokenExchange`. Grant scoped deployment and registry push/pull permissions, not subscription Owner; the release workflow does not change foundation RBAC. Set environment variables `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`, `RESOURCE_PREFIX`, `AZURE_REGISTRY`, `AUTH0_ISSUER`. The manual workflow runs CI first, builds on the GitHub runner, pushes to ACR, scans the exact release digest, deploys that digest, then checks discovery and the anonymous-auth boundary. Pin Actions and container base images to reviewed digests/commit SHAs under your supply-chain policy before enabling unattended release. Dependabot tracks updates.

## 5. Grant the owner and connect ChatGPT

Grant the operator identity **Storage Table Data Contributor** and **Storage Blob Data Contributor** on this storage account. The runtime identity intentionally cannot grant access. Prepare a separate protected operator environment with the deployment settings, including the issuer and table/blob endpoints, then use:

```bash
uv run fv-data-mcp grant --subject 'auth0|YOUR_USER_ID'
uv run fv-data-mcp backup-members
```

See [operations](operations.md) for an operator environment that needs no Finviz/Redis credentials. In ChatGPT's available custom MCP/app connection settings, enter the deployed **HTTPS `/mcp` URL**, OAuth client credentials, and authenticate as the invited owner. Availability depends on your ChatGPT plan/workspace permissions. Enable the relevant tools, then ask for an explicit listed expiration first. You should see twelve read-only tools. No OpenAI API key is needed by this server.

## Release gates

Do not claim the deployment is production-accepted until these pass:

- [ ] Live Finviz account: listed expirations, chain CSV/IV units, timestamped prices, statements, overview and news all match adapters and licensing allows invited-user access. Unsupported HTML listing fails explicitly; do not silently guess dates.
- [ ] Actual Upstash: two replicas coalesce same-key misses; quota/cooldown/cancellation tests; backend consistency and plan limits accepted. Never run destructive limiter tests against the active provider-account namespace.
- [ ] Auth0 → ChatGPT end-to-end: audience, issuer, PKCE, scope, callback/registration, token refresh if configured; wrong-audience/expired/uninvited tokens denied; suspension blocks warm-cache access.
- [ ] Docker build and image scan; templates compiled and `what-if` reviewed; managed identity works without operator credentials; startup/cold-start timing acceptable.
- [ ] Stale/Redis outage/provider 429 drills, bounded responses and cursor continuation tested against deployed HTTPS ingress.
- [ ] Logs, traces, metrics and alert email delivered; quota alert, budget and log-cap behavior understood. Automated monitoring must not unintentionally keep min=0 replicas warm.
- [ ] Access backup exists and restore drill succeeds; rollback digest recorded; MFA, secret rotation, support owner and data-retention responsibilities assigned.

Local validation cannot establish these cloud/provider acceptance conditions. Docker Desktop's WSL integration must be enabled if Docker commands report no engine in this environment.
