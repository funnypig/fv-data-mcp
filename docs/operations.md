# Operations runbook

## Access lifecycle

Use Auth0 for user invitations and identity verification; record exact `sub`, not a mutable email address. Access records are keyed by SHA-256 of issuer and subject in separate Table partition/row keys. Runtime can read only. All administration requires an Azure operator identity with Table write rights, and Blob write rights for backups.

For CLI-only administration without provider secrets, set these in a protected operator shell/environment file:

```text
FV_ENVIRONMENT=development
FV_PUBLIC_URL=http://localhost:8000
FV_DEV_AUTH_ENABLED=true
FV_DEV_TOKEN=<random-local-only-value-at-least-24-characters>
FV_CURSOR_SECRET=<random-value-at-least-32-characters>
FV_ACCESS_BACKEND=azure
FV_AUTH0_ISSUER=https://YOUR-TENANT.eu.auth0.com/
FV_TABLE_ENDPOINT=https://STORAGE.table.core.windows.net/
FV_BLOB_ENDPOINT=https://STORAGE.blob.core.windows.net/
```

Run `az login` first. This environment is for CLI commands **only**, never the deployed server. The real issuer is still used for access record keys. No API credential is needed for these commands.

```bash
uv run fv-data-mcp grant --subject 'auth0|USER_ID'
uv run fv-data-mcp suspend --subject 'auth0|USER_ID'
uv run fv-data-mcp backup-members
```

Suspend membership immediately for a compromised user, and revoke Auth0 sessions/refresh tokens as well. Subsequent calls are blocked even if their market data is cached. In-flight calls may finish. Deleting/blocking only the Auth0 user does not revoke an already-issued JWT at this resource server. There is no per-user cache purge because market data is shared and carries no user-private payload.

## Backups and recovery

Run `backup-members` **after every access change** and at least daily through an operator-owned scheduler with managed/workload identity. A schedule is not provisioned by these templates; configure and monitor one before multi-user launch. Blob snapshots have 30-day lifecycle retention plus soft-delete/version retention. Backups contain identity subjects: restrict access and treat them as personal data. No market history is backed up; Redis snapshots are disposable.

To recover a lost/corrupted membership table, recreate the table using the foundation template, then:

```bash
uv run fv-data-mcp restore-members --blob 'members/YYYY-MM-DDTHH-MM-SSZ.json'
```

Restore checks the issuer and **restores every listed user disabled**, even if the backup says enabled. Verify current entitlements and selectively `grant` afterward; an old backup must not re-enable a revoked user. Existing rows not present in the backup are untouched. When recovering suspected unauthorized grants, quarantine the old table, point `FV_ACCESS_TABLE` at a clean table and review the full roster first. A restore is not transactional; repeat after a partial failure, then validate each intended user. Do not use it as a general live-table rollback. Recovery-point objective is the last successful backup; recovery-time target must be measured in a cloud drill, not assumed.

## Observability

Application stdout is structured JSON: event, request ID, tool name, status, error code and duration; current trace ID is included when present. It does not log bearer tokens, subjects, raw questions, tickers, response bodies or upstream URLs. Third-party HTTP/SDK logs are suppressed to prevent URL/API-key leakage. Raw exception strings/stacks are deliberately omitted. Debugging should use reproducible sanitized fixtures, not production payload logging.

OpenTelemetry export is enabled by `FV_APPLICATIONINSIGHTS_CONNECTION_STRING`. Default trace sampling 10%; explicit tool spans only, no automatic HTTP instrumentation that could collect Finviz query credentials. Metrics:

- `fv.tool.calls`: tool/status/error-code counts.
- `fv.tool.duration`: seconds histogram for warm/cold/error requests.
- `fv.provider.attempts`, `fv.provider.responses`: Finviz endpoint and status, without query parameters.
- `fv.source.access`: returned source provenance grouped by provider/cache hit/staleness; a tool returning several snapshots increments several times. This is not raw Redis command billing.

In the Container Apps Log Analytics workspace:

```kusto
ContainerAppConsoleLogs_CL
| extend event = parse_json(Log_s)
| where event.event == "tool_completed"
| summarize calls=count(), errors=countif(event.status == "error"),
    p95_ms=percentile(todouble(event.duration_ms), 95) by tostring(event.tool), bin(TimeGenerated, 5m)
```

The foundation creates an email alert for at least five tool errors in five minutes and budget notifications. Add provider-429/stale-ratio/latency alerts from the exported metrics after establishing baseline traffic; no-traffic is normal with scale-to-zero. Verify table schema and email delivery in the target Azure region. Log cap exhaustion stops useful diagnostics, so monitor workspace ingestion/cap notifications. One deliberate deployment auth smoke check is preferable to frequent external keepalive pings if cost is the priority.

## Failure playbooks

| Symptom | Safe response |
| --- | --- |
| Redis timeout/unavailable | Tools fail closed. Restore cache service/credentials; do not bypass cache or limiter. Investigate eviction, outages and connection limits. |
| Finviz 429 | Respect shared cooldown, inspect provider attempt metrics; ensure no other app uses the same upstream key. Reduce configured budget if the account's actual limit is lower. |
| Stale result | Inspect `source_as_of`, `cache_age_seconds`, `source_delay_seconds`, `refresh_error`; never describe it as live data. Hard-expired snapshots cannot be served. |
| `provider_schema_changed` | Quarantine a sanitized provider response for parser tests; do not turn parse errors into zeros or remove validation. |
| Expiration discovery unsupported | Ask for an explicit verified listed date; update adapter using captured page data. Never infer all Fridays. |
| `gamma_flip: unavailable` | Check coverage, dividend/rate inputs, Treasury observation date. Explicit rate/dividend overrides must come from the caller, not an invisible zero default. |
| 401 | Check token issuer/audience/expiry/signing key and the discovery URL. Never accept an ID token or disable JWT validation. |
| 403 | Check scope and issuer/subject membership; confirm suspension intent before granting. |
| Large payload | Smaller chain page or fewer expirations; do not raise global limits blindly. |
| Cold-start timeout | Retry once and measure. Consider min replicas 1 only after accepting idle compute cost. |

After Redis failover/state loss, pause provider-bound traffic for at least the quota window. Existing limiter state may not reflect upstream requests; eventual consistency is not a strong quota guarantee. Do not perform `FLUSHDB`/`FLUSHALL` during live traffic. Use a new isolated database/namespace for destructive drills.

## Rollback and rotation

Record the previous deployed **image digest** and environment configuration before releases. Roll back with the same `infra/app.bicep` deployment command, the prior digest and unchanged issuer/account parameters. Single-revision mode shifts traffic when the new revision is ready; confirm readiness and authenticated smoke tests. If a release changes cache or calculation schema, bump namespace/method version, and retain compatibility until old cursors expire. Rollback must also account for schema changes; this version has no membership migration.

Rotate Finviz/Redis secrets in Key Vault, refresh/restart revisions and verify readiness plus an authenticated read. Keep the provider-account namespace stable across replicas and rotations. Rotate the cursor secret on compromise; this intentionally invalidates outstanding cursors, not access tokens. Auth0 signing-key rotation is handled by JWKS caching; unknown-key refresh has a 30-second abuse cooldown and can briefly reject a legitimate new key. Maintain vault purge protection. For an emergency service stop, disable ingress or deactivate the active revision, rather than merely setting min replicas to zero.

## Release discipline

Run tests, lint, lockfile validation and vulnerability/image scans before deployment. Review automated dependency PRs, especially MCP/Starlette/HTTPcore changes. CI currently uses version-tagged Actions; pin reviewed immutable Action SHAs and base-image digests under the deployment policy. No unattended deployment occurs: workflow dispatch and protected-environment approval are required. The test suite includes a 100-caller shared-fetch regression, mocked ASGI MCP calls, JWT adversarial claims, source pagination, numeric invariants and SSRF cases; this is not a soak test or a substitute for a deployed load/chaos test.
