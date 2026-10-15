from __future__ import annotations

import asyncio
import hashlib
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass

import httpx
import jwt
from azure.core.exceptions import AzureError, ResourceNotFoundError
from azure.data.tables.aio import TableClient
from azure.identity.aio import DefaultAzureCredential
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

from fv_data_mcp.config import Settings
from fv_data_mcp.errors import ResearchError, unavailable


@dataclass(frozen=True)
class Principal:
    """Identify an authenticated user by trusted issuer and immutable subject."""

    issuer: str
    subject: str

    @property
    def key(self) -> str:
        """Return the issuer-qualified identity used to partition user limits."""
        return self.issuer + "|" + self.subject


principal_context: ContextVar[Principal | None] = ContextVar("principal", default=None)
request_context: ContextVar[str] = ContextVar("request_id", default="")


def member_keys(issuer: str, subject: str) -> tuple[str, str]:
    """Hash issuer and subject into Azure Table partition and row keys."""
    return hashlib.sha256(issuer.encode()).hexdigest(), hashlib.sha256(subject.encode()).hexdigest()


class Memberships:
    """Read and administer the explicit user-access allowlist."""

    credential: DefaultAzureCredential | None
    table: TableClient | None

    def __init__(self, settings: Settings) -> None:
        """Choose Azure storage or an isolated development-only in-memory allowlist."""
        self.memory: dict[str, bool] = {}
        self.credential = None
        self.table = None
        if settings.access_backend == "azure":
            self.credential = DefaultAzureCredential()
            self.table = TableClient(
                settings.table_endpoint, settings.access_table, credential=self.credential
            )
        if settings.dev_auth_enabled:
            self.memory[Principal("local", "developer").key] = True

    async def allowed(self, principal: Principal) -> bool:
        """Check current membership, denying missing users and failing closed on outages."""
        if self.table is None:
            return self.memory.get(principal.key, False)
        try:
            partition, row = member_keys(principal.issuer, principal.subject)
            entity = await self.table.get_entity(partition, row)
            return entity.get("enabled") is True
        except ResourceNotFoundError:
            return False
        except AzureError:
            raise unavailable("User access records are unavailable.") from None

    async def set_access(self, principal: Principal, enabled: bool) -> None:
        """Persist an operator-authorized grant or suspension for one identity."""
        if self.table is None:
            self.memory[principal.key] = enabled
            return
        partition, row = member_keys(principal.issuer, principal.subject)
        await self.table.upsert_entity(
            {
                "PartitionKey": partition,
                "RowKey": row,
                "enabled": enabled,
                "issuer": principal.issuer,
                "subject": principal.subject,
            }
        )

    async def close(self) -> None:
        """Release the table client and its asynchronous Azure credential."""
        if self.table:
            await self.table.close()
        if self.credential:
            await self.credential.close()


class Authenticator:
    """Validate bearer credentials and enforce scope plus invited membership."""

    settings: Settings
    client: httpx.AsyncClient
    members: Memberships
    loaded_at: float
    attempted_at: float
    lock: asyncio.Lock

    def __init__(self, settings: Settings, client: httpx.AsyncClient, members: Memberships) -> None:
        """Initialize identity dependencies and bounded signing-key refresh state."""
        self.settings, self.client, self.members = settings, client, members
        self.keys: dict[str, jwt.PyJWK] = {}
        self.loaded_at = -float("inf")
        self.attempted_at = -float("inf")
        self.lock = asyncio.Lock()

    async def _key(self, kid: str) -> RSAPublicKey:
        """Resolve an RSA verification key with caching and unknown-key refresh throttling."""
        async with self.lock:
            now = time.monotonic()
            if kid in self.keys and now - self.loaded_at < 3600:
                return self.keys[kid].key
            if now - self.attempted_at < 30:
                raise ResearchError("invalid_token", "Token signing key is unavailable.")
            self.attempted_at = now
            try:
                async with self.client.stream(
                    "GET", self.settings.auth0_issuer + ".well-known/jwks.json", timeout=5
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 65536:
                            raise ValueError
                import json

                document = json.loads(body)
                self.keys = {
                    item["kid"]: jwt.PyJWK.from_dict(item, algorithm="RS256")
                    for item in document["keys"]
                    if item.get("kty") == "RSA"
                    and item.get("use", "sig") == "sig"
                    and item.get("kid")
                }
                self.loaded_at = now
            except (httpx.HTTPError, ValueError, KeyError, TypeError, jwt.PyJWTError):
                raise unavailable("Identity signing keys could not be verified.") from None
            if kid not in self.keys:
                raise ResearchError("invalid_token", "Token signing key is unknown.")
            return self.keys[kid].key

    async def authenticate(self, token: str) -> Principal:
        """Validate a bearer token and return an authorized principal.

        Raises:
            ResearchError: Credentials, scope, membership, or verification services fail.
        """
        if len(token) > 16384:
            raise ResearchError("invalid_token", "Invalid bearer token.")
        if self.settings.dev_auth_enabled:
            if not secrets.compare_digest(token, self.settings.dev_token.get_secret_value()):
                raise ResearchError("invalid_token", "Invalid development token.")
            principal = Principal("local", "developer")
        else:
            try:
                header = jwt.get_unverified_header(token)
                if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                    raise jwt.InvalidTokenError
                key = await self._key(header["kid"])
                claims = jwt.decode(
                    token,
                    key,
                    algorithms=["RS256"],
                    audience=self.settings.auth0_audience,
                    issuer=self.settings.auth0_issuer,
                    leeway=5,
                    options={"require": ["exp", "iat", "iss", "aud", "sub"]},
                )
                if not isinstance(claims["sub"], str) or not claims["sub"]:
                    raise jwt.InvalidTokenError
            except (jwt.PyJWTError, ValueError, TypeError):
                raise ResearchError("invalid_token", "Bearer token validation failed.") from None
            scope = claims.get("scope", "")
            if not isinstance(scope, str) or "research:read" not in scope.split():
                raise ResearchError("insufficient_scope", "The research:read scope is required.")
            principal = Principal(claims["iss"], claims["sub"])
        if not await self.members.allowed(principal):
            raise ResearchError(
                "access_denied", "This account has not been invited or has been suspended."
            )
        return principal
