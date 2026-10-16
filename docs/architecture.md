# Architecture and trade-offs

See [Authentication and access](authentication.md) for Mermaid diagrams explaining
Auth0 accounts, OAuth clients, MCP users, Azure identities and membership grants.

```text
ChatGPT ── OAuth code + PKCE ── Auth0
   │ bearer token
   ▼
Container Apps HTTPS ingress → MCP /mcp (stateless JSON responses)
   │ JWT + audience + scope + invited membership
   ├── Azure Table: access records, read-only runtime identity
   ├── Redis: shared snapshots, miss locks, quotas, cursors' source data
   └── Providers: Finviz / Treasury / public article publishers
          ▲ source cache miss + quota reservation only

Key Vault → managed identity secret references
JSON stdout → Log Analytics; explicit OTel spans/metrics → Application Insights
```

## Why this deployment

The app uses **Consumption, min replicas 0, max replicas 2**, not an always-on VM. Idle application compute can scale to zero; the first request then pays cold-start latency. Redis is external and survives scale-to-zero. There are no overnight fetches, market polling jobs, or historical archives.

Upstash pay-as-you-go avoids provisioning a dedicated Azure Redis instance for a small private service. Azure Table is the inexpensive durable key/value alternative to DynamoDB here; it stores only invited-user records, not market snapshots. Blob Storage holds access backups. Redis is appropriate for short TTLs, locks and Lua-based counters; Table Storage alone would require materially different cache/coordination code.

Not everything is free while compute sleeps: registry storage, retained logs, secrets, storage transactions, cache traffic and Auth0/provider plans can still cost money. Redis cache hits also incur commands; miss waiters poll at 100 ms, so large cold bursts amplify command usage. Probes and external uptime pings can incur work and prevent scale-to-zero. The default Azure budget notification is **50 subscription billing-currency units/month**, not a hard spending cap or price guarantee. Validate the Azure calculator, Upstash/Auth0 plans, provider entitlements and telemetry volume before provisioning. No premium Redis availability add-on, private endpoints, WAF or multi-region failover is included.

## Source caching, not only tool caching

Finviz requests use authenticated export endpoints, not browser stock pages:
`/export/options`, `/quote_export`, `/api/statement`, `/news_export`, and `/export`.
Expiration discovery reads the all-expiration options CSV and cross-checks listed
dates against contract symbols; it never invents a Friday schedule. This discovery
download is bounded by the same response-size limit and cached request coordination.
Overview combines screener views 111 and 161 (company/valuation and financial metrics).
Descriptions and named institutional holders are unavailable from these exports and
are explicitly reported as such. Missing dividend yield remains unknown, never zero.
Intraday export timestamps without a timezone are interpreted in America/New_York,
including the provider's minute-resolution AM/PM format.
HTTP redirects remain blocked (including cross-origin redirects) and are reported as
`provider_endpoint_changed`, not invalid credentials. A 401/403 means upstream access
was denied; it can reflect credentials or endpoint entitlement. Credentials are sent
only to the fixed Elite origin, never forwarded to redirected public pages.

`get_option_chain`, `get_max_pain`, `get_gamma_exposure`, `get_option_metrics` and `get_options_summary` all read the same `FinvizProvider.chain(ticker, expiration)` cache. Filters run on the cached full chain; changing a display filter does not create another provider request. Fundamentals reused for dividend input share their own cache.

Below normalized datasets, `FinvizProvider._get` caches complete successful HTTP bodies
for 300 seconds, with no raw stale fallback. Its account-scoped identity includes GET,
origin, path, and canonical wire parameters. Direct repeated calls and concurrent calls
from separate provider instances share one Redis miss lock around the entire retry
sequence. Credentials are added only when sending the request. This is response reuse
and distributed single-flight, not an upstream idempotency-key or exactly-once guarantee.
Normalized snapshots inherit the raw response's original fetch timestamp, so rebuilding
a dataset cannot restart freshness or retention. Dataset-specific retention still governs
normalized snapshots; raw bodies are a shorter-lived optimization, not a separate source.
Raw bodies add Redis storage/traffic (including immutable snapshot copies); monitor cache
size and command costs. Non-success statuses never become successful cached bodies;
existing negative caching handles transient errors and not-found responses separately.

Key scope: schema namespace + hashed provider-account identity + dataset + canonical parameters. Never put API credentials in keys. All replicas using one upstream account must use the **same** `FV_PROVIDER_ACCOUNT_ID` and Redis database. Market data is shared only across authorized users of that one account. A future bring-your-own-key or licensed multi-tenant mode must partition both source caches and quotas by tenant/entitlement; it is not implemented.

| Dataset | Fresh | Hard lifetime from successful fetch |
| --- | --- | --- |
| Chain / five-minute stock price / news | 300 sec | 900 sec |
| Listed expirations | 24 hr, keyed by New York date | 24 hr |
| Company overview | 1 hr | 1 hr |
| Quarterly statements / extracted articles | 24 hr | 24 hr |
| Treasury curve | 24 hr | 7 days; latest observation must also be no older than 7 days |

For source misses, `SET NX` chooses a refresher; token-checked Lua publication and release prevent a late worker overwriting a newer snapshot. Other callers wait up to 25 seconds. Lock lease 35 seconds exceeds the default 30-second tool deadline. Transient failures have a short shared negative cache and can return still-retained stale data; authentication/schema errors do not silently use stale data. Redis errors fail closed, including warm-cache reads; there is no unmetered direct-provider fallback.

Each source loader also has its own 30-second deadline, including nested response-cache
waits, so direct provider callers cannot keep doing network work past a refresh lease.

Immutable source IDs retain exact chain pages across refreshes. HMAC cursors bind ticker, expiry, filters, page size, offset, source ID and absolute expiry. Cursors are integrity-protected, **not credentials**; every page must authenticate. Derived keys include method version, input snapshot IDs and all model parameters; GEX additionally includes minute-resolution valuation time. Derived TTL cannot exceed the shortest input lifetime. Calculation hits still verify source snapshots have not expired.

Cache age is not market-data age: option exports are labeled with a 900-second provider-delay assumption and unknown quote/OI timestamps. Bar-close timestamps are exposed separately. Freshly fetching yesterday's closing bar does not make it a live quote.

## Quotas and failure behavior

Default Finviz budget: 8 requests in a rolling 5-second Redis-TIME window, including retries and all endpoint types. Reserve **after acquiring local concurrency capacity**, immediately before the network call. A shared `Retry-After` cooldown stops sibling tools from retrying a throttled account. At most two attempts, three concurrent Finviz requests per process, 30 tool calls/minute and two active tools/user. User leases expire after 65 seconds to recover from worker loss. Request body 64 KiB, tool response budget 256 KiB, provider body 8 MB, public article body 2 MB. Tool execution has a 30-second deadline; authentication adds its own bounded verification time.

Finviz's named request constants and `FINVIZ_RETRY_POLICY` define a 12-second attempt
deadline (including local concurrency/quota waits), two attempts, full-jitter exponential
backoff starting at 0.3 seconds with a 5-second cap, and a 30-second total budget. The
shared `retry_async` decorator retries only explicitly marked transient failures; each
attempt releases its stream and concurrency slot before sleeping and reserves fresh
quota. HTTP 429 and server failures with Retry-After publish account-wide cooldowns;
delays beyond five seconds return retry advice instead of sleeping or retrying early.
Cancellation, authentication, non-transient client errors, and oversized bodies are not
retried. HTTPX transport-level retries remain disabled to avoid hidden attempts.

**Consistency caveat:** Upstash documents eventual consistency. Lua/NX coordination is tested on Redis semantics, not a formal global lock guarantee across an Upstash failover. Use a single primary-region database, no read-region routing, persistence and no eviction of coordination keys; validate these features for the selected plan. Do not share the Finviz key with another application. The safety margin is not a proof of strict quota compliance during state loss or network partition. If strict cross-failure quota enforcement is contractual, replace Upstash with a reviewed strongly coordinated backend or single upstream-request broker before launch. After Redis restoration/failover, pause provider traffic for at least one quota window and investigate before resuming. Never flush limiter keys during live traffic.

## Trust boundaries

- Auth0 is the authorization server; this service never receives user passwords or handles authorization codes. JWTs accept RS256 only; exact issuer, resource audience, expiry, issued-at and `research:read` are checked. Unknown signing keys cause at most one JWKS refresh per process per 30 seconds; known keys cache for one hour.
- Azure Table membership is checked on each HTTP request and again before tool execution, without a membership cache. Suspension affects subsequent calls immediately, though already-running calls can finish. Auth0 account deletion alone does not instantly invalidate a previously issued JWT; suspend membership too.
- Runtime identity has Table **reader**, vault secret reader and registry pull rights. Grant/suspend/restore run from an operator identity, never an MCP tool. No administration HTTP endpoints exist.
- Public discovery/probes return no credentials or account data. Host and Origin validation, SDK body caps and bearer-only auth apply to MCP. No wildcard CORS. The local token mode is rejected with production settings and must never be publicly proxied.
- Article IDs are derived from validated news URLs and stored for 24 hours. Only HTTPS:443/public IPs are allowed; every redirect and every DNS result is checked. The network backend connects to the validated IP while HTTPcore retains the original TLS hostname. IPv4-mapped IPv6 and Azure metadata endpoints are blocked. Optional publisher-domain allowlist is supported. No arbitrary URL argument, cookie forwarding, paywall bypass, or credentials forwarded to publishers.
- Logs deliberately omit user subjects, ticker/query bodies, bearer tokens, full upstream URLs and raw exception strings. News and article text remain untrusted external data; clients must not treat them as instructions.

References: [Container Apps scaling](https://learn.microsoft.com/en-us/azure/container-apps/scale-app), [Upstash consistency](https://upstash.com/docs/redis/features/consistency), [Azure Table roles](https://learn.microsoft.com/en-us/azure/role-based-access-control/built-in-roles/storage#storage-table-data-reader).
