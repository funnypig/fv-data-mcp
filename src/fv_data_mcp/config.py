from __future__ import annotations

from typing import ClassVar, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Load environment-backed settings and reject unsafe runtime combinations."""

    model_config: ClassVar[SettingsConfigDict] = SettingsConfigDict(
        env_prefix="FV_", env_file=".env", extra="ignore", hide_input_in_errors=True
    )

    environment: Literal["development", "test", "production"] = "development"
    public_url: str = "http://localhost:8000"
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    provider_account_id: str = "owner"
    finviz_api_key: SecretStr = SecretStr("")
    auth0_issuer: str = ""
    auth0_audience: str = ""
    cursor_secret: SecretStr = SecretStr("")
    access_backend: Literal["memory", "azure"] = "memory"
    table_endpoint: str = ""
    blob_endpoint: str = ""
    access_table: str = "members"
    dev_auth_enabled: bool = False
    dev_token: SecretStr = SecretStr("")
    request_timeout: float = Field(default=30, gt=0, le=60)
    max_body_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_response_bytes: int = Field(default=262144, ge=4096, le=1048576)
    max_provider_bytes: int = Field(default=8_000_000, ge=1024, le=20_000_000)
    provider_limit: int = Field(default=8, ge=1, le=8)
    provider_window_seconds: int = Field(default=5, ge=5)
    user_calls_per_minute: int = Field(default=30, ge=1, le=600)
    user_concurrency: int = Field(default=2, ge=1, le=10)
    article_allowed_domains: list[str] = Field(default_factory=list)
    applicationinsights_connection_string: SecretStr = SecretStr("")
    trace_sample_ratio: float = Field(default=0.1, ge=0, le=1)

    @property
    def resource_url(self) -> str:
        """Return the canonical MCP resource URL used as the OAuth audience."""
        return self.public_url.rstrip("/") + "/mcp"

    @model_validator(mode="after")
    def validate_runtime(self) -> Settings:
        """Validate deployment origins, credentials, and backend safety requirements."""
        public = urlsplit(self.public_url)
        if (
            public.scheme not in {"http", "https"}
            or not public.hostname
            or public.username
            or public.password
        ):
            raise ValueError("public_url must be an absolute HTTP(S) URL")
        if public.path not in {"", "/"} or public.query or public.fragment:
            raise ValueError("public_url must be an origin without a path or query")
        if len(self.cursor_secret.get_secret_value()) < 32:
            raise ValueError("cursor_secret must contain at least 32 characters")
        if self.dev_auth_enabled:
            if self.environment == "production" or public.hostname not in {
                "localhost",
                "127.0.0.1",
            }:
                raise ValueError("Development authentication is restricted to local development")
            if len(self.dev_token.get_secret_value()) < 24:
                raise ValueError("dev_token must contain at least 24 characters")
        else:
            issuer = urlsplit(self.auth0_issuer)
            if (
                issuer.scheme != "https"
                or not issuer.hostname
                or not self.auth0_issuer.endswith("/")
            ):
                raise ValueError("auth0_issuer must be an HTTPS issuer ending in /")
            if (
                issuer.path != "/"
                or issuer.query
                or issuer.fragment
                or issuer.username
                or issuer.password
            ):
                raise ValueError("auth0_issuer must be an Auth0 tenant origin")
            if self.auth0_audience != self.resource_url:
                raise ValueError("auth0_audience must equal public_url + /mcp")
        if self.environment == "production":
            if public.scheme != "https" or self.access_backend != "azure":
                raise ValueError("Production requires HTTPS and Azure-backed access records")
            if not self.redis_url.get_secret_value().startswith("rediss://"):
                raise ValueError("Production Redis connections require TLS")
            if not self.finviz_api_key.get_secret_value():
                raise ValueError("Production requires a Finviz API key")
        if self.access_backend == "azure" and not self.table_endpoint.startswith("https://"):
            raise ValueError("Azure access records require an HTTPS table_endpoint")
        return self
