# Validation record

Local implementation validation, 2026-10-13:

- Python suite: 69 tests, including MCP HTTP integration, 100 concurrent cache misses, JWT rejection cases, startup client construction, numerical checks and SSRF defenses. Coverage exceeds the enforced 80% CI threshold.
- Ruff lint and formatting checks.
- Locked dependency audit: no known vulnerabilities reported after upgrading Starlette. The unpublished local application is not in the advisory database and is not itself certified by this scan.
- Both Azure Bicep templates compile with Bicep CLI 0.48.1; YAML parses successfully. Compilation is not a cloud `what-if` or deployment validation.
- Azure async client construction is tested without obtaining credentials or contacting Azure.

Not validated in this environment: Docker image build/runtime (Docker Desktop WSL integration is unavailable), image vulnerability scan, actual Azure resources/managed identities, Upstash network/failover semantics, live Finviz/Treasury/publisher responses, and ChatGPT/Auth0 end-to-end connection. The included CI and deployment workflow run additional checks when configured. See the deployment release gates before granting other users access.

No cloud resources were provisioned and no provider credentials from the original repository were read or copied. No changes are required in the original application's tracked files.
