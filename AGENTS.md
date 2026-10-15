# Agent Instructions

These rules apply to every Python file in this repository, including application code,
tests, fixtures, scripts, private helpers, and nested functions. Follow them when adding
or modifying code; do not weaken the rules to accommodate an implementation.

## Required Documentation and Typing

- Every class must have a docstring explaining its purpose and responsibilities.
- Every function and method must have a docstring, including asynchronous functions,
  constructors (`__init__`), properties, private helpers, callbacks, and test functions.
- Every parameter must have an explicit type annotation, including `*args` and
  `**kwargs`. The conventional `self` and `cls` parameters do not need annotations.
- Every function and method must have an explicit return annotation. Use `-> None`
  when nothing is returned, including constructors and tests that return no value.
- Annotate class/data-model fields. Classes themselves do not have return annotations;
  their constructors and methods do.
- For `async def`, annotate the value produced by awaiting the function, not
  `Coroutine[...]`. For generators and context-manager factories, annotate the
  appropriate `Iterator`, `Generator`, `AsyncIterator`, or `AsyncGenerator` type.
- Use precise types and parameterized collections (`list[str]`, `dict[str, float]`),
  unions for optional values, and typed callable signatures. Use `Any` only at genuinely
  dynamic boundaries; never add it merely to avoid choosing a meaningful type.
- Prefer named, documented functions over lambdas for nontrivial logic or reusable
  callbacks. Python lambdas cannot carry parameter/return annotations or docstrings;
  keep any unavoidable inline lambdas trivial and covered by the surrounding contract.

## Docstring Style

Use triple-double-quoted docstrings as the first statement in the definition. Start
with a concise, meaningful summary. For nontrivial interfaces, use Google-style
`Args`, `Returns` (or `Yields`), and `Raises` sections as applicable. Explain behavior,
units, assumptions, side effects, and constraints rather than restating the name or
duplicating type annotations. Preserve existing MCP tool descriptions: their docstrings
are part of the client-facing interface.

```python
class QuoteService:
    """Provide cached stock-price research data."""

    def __init__(self, currency: str) -> None:
        """Initialize the service with its reporting currency."""
        self.currency: str = currency

    def supported_currencies(self) -> list[str]:
        """Return the currencies supported by this service."""
        return [self.currency]
```

## Before Finishing a Change

1. Check every added or modified class/function for a meaningful docstring.
2. Check parameter, field, and return annotations, including private/nested definitions.
3. Keep typing/documentation changes behavior-preserving unless a behavior change was
   requested. Do not change authentication, caching, or calculation semantics just to
   simplify annotations.
4. Run the repository's relevant checks: `uv run ruff check .`,
   `uv run ruff format --check .`, and applicable `uv run pytest` tests. Report checks
   that could not be run. Do not claim annotations alone establish type correctness.
5. Never read, copy, log, or commit credentials merely to document or type an interface.

## Automated Enforcement

Ruff enforces missing parameter/return annotations and public class/function docstrings.
`tests/test_code_standards.py` additionally checks private and nested definitions,
constructors, and assigned class/instance fields across repository-owned Python files.
Run `uv run pytest tests/test_code_standards.py` when changing these rules. Keep these
checks enabled in the existing CI; do not add blanket suppressions to bypass them.

## External Requests, Caching, and Replay Safety

- Distinguish HTTP idempotency (replaying does not add side effects) from caching
  (reusing a result) and single-flight coordination (coalescing concurrent misses).
  GET semantics alone do not prevent duplicate quota consumption. Do not invent an
  idempotency-key header for an upstream that does not support it.
- Put shared response caching and distributed miss coordination at the lowest reusable
  outbound-fetch boundary, not only around tools or calculations. Direct callers and
  different workflows requesting the same resource must share this protection.
- Identify requests by method, origin/path, canonical parameters, representation, and
  account/tenant/entitlement scope. Never put credentials in keys, logs, or metrics.
  Do not share private results across authorization boundaries.
- Specify freshness, retention, negative caching, and stale fallback explicitly.
  Reusing cached input must preserve its original fetch time and lifetime; an outer
  cache must not make old data appear newly fetched or extend its absolute lifetime.
- Only cache complete successful responses as data; keep temporary failure suppression
  separate. Fail closed if quota/cache coordination is unavailable. Every actual
  network attempt, including retries, must reserve quota and emit attempt telemetry.
- Coordinate the entire retry sequence under the shared miss lock. Bound operation
  deadlines below lock leases; release ownership on failure/cancellation and prevent
  expired owners from publishing. Do not claim distributed exactly-once execution.

## Retry Policies and Operational Constants

- Keep retry loops out of provider/business methods. Prefer an existing suitable
  library capability; otherwise use one reusable, typed async decorator/helper.
  Do not stack transport retries with application retries and multiply attempts.
- Retry only explicitly classified transient failures of replay-safe operations.
  Writes require an upstream-supported idempotency contract before automatic replay.
  Never retry cancellation, validation/auth failures, or programming errors blindly.
- Define bounded attempts, per-attempt and total deadlines, capped exponential
  backoff with jitter, and server-directed Retry-After behavior in a named policy.
  Do not shorten a server delay to fit the retry budget; return retry advice instead.
- Close streams and release concurrency capacity before retry sleeps. Preserve
  cancellation, safe errors, callable metadata, and type signatures in decorators.
- Replace unexplained operational literals (timeouts, attempts, concurrency, TTLs,
  byte limits, cooldowns) with descriptive constants or validated configuration.
  Include units in names and keep related policy values together. Protocol constants
  such as HTTP status codes do not need pointless aliases.
- Test request reuse across callers/replicas, parameter and account isolation,
  expiry/provenance, bounded retries, Retry-After, per-attempt quota accounting,
  permanent failures, resource cleanup, and cancellation. Document the resulting
  behavior and any operational trade-offs.
