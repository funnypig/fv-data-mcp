from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable, Iterator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from fv_data_mcp.cache import SnapshotCache
from fv_data_mcp.errors import ResearchError
from fv_data_mcp.providers.articles import ArticleProvider, PublicNetworkBackend, public_address


class Response:
    """Supply a bounded in-memory HTTP response for article-fetch tests."""

    status: int
    headers: list[tuple[bytes, bytes]]
    body: bytes

    def __init__(
        self, status: int = 200, headers: list[tuple[bytes, bytes]] | None = None, body: bytes = b""
    ) -> None:
        """Store the fake status, headers, and response bytes."""
        self.status = status
        self.headers = headers or [(b"content-type", b"text/html")]
        self.body = body

    async def aiter_stream(self) -> AsyncIterator[bytes]:
        """Yield the configured fake response body as one asynchronous chunk."""
        yield self.body


@pytest.mark.parametrize("address", ["224.0.0.1", "ff02::1", "127.0.0.1", "::1", "168.63.129.16"])
def test_nonpublic_or_multicast_destinations(address: str) -> None:
    """Reject local, metadata, and multicast IPs even when address flags are misleading."""
    assert not public_address(address)


class Pool:
    """Serve predetermined responses while recording attempted article destinations."""

    responses: Iterator[Response]
    visited: list[str]

    def __init__(self, responses: Iterable[Response]) -> None:
        """Queue fake responses and initialize the visited-URL log."""
        self.responses = iter(responses)
        self.visited = []

    @asynccontextmanager
    async def stream(self, method: str, url: str, **kwargs: object) -> AsyncIterator[Response]:
        """Record a request and yield the next prepared response."""
        self.visited.append(url)
        yield next(self.responses)

    async def aclose(self) -> None:
        """Complete fake transport cleanup without opening network resources."""
        pass


async def test_redirect_to_metadata_is_blocked_before_connection(redis: FakeRedis) -> None:
    """Reject a redirect to metadata before a second transport connection is attempted."""
    pool = Pool([Response(302, [(b"location", b"https://169.254.169.254/latest")])])
    provider = ArticleProvider(SnapshotCache(redis, "account"), [], pool=pool)
    with pytest.raises(ResearchError, match="Non-public"):
        await provider._download("https://example.com/story")
    assert len(pool.visited) == 1


async def test_dns_pinning_and_mixed_public_private_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect to the validated public IP and reject mixed public/private DNS results."""
    backend = PublicNetworkBackend()
    backend.backend.connect_tcp = AsyncMock(return_value="stream")
    resolver = AsyncMock(return_value=[(2, 1, 6, "", ("93.184.216.34", 443))])
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolver)
    assert await backend.connect_tcp("example.com", 443) == "stream"
    assert backend.backend.connect_tcp.call_args.args[0] == "93.184.216.34"
    resolver.return_value.append((2, 1, 6, "", ("127.0.0.1", 443)))
    with pytest.raises(ResearchError, match="non-public"):
        await backend.connect_tcp("example.com", 443)
    assert backend.backend.connect_tcp.call_count == 1


async def test_article_extraction_caches_shared_content(redis: FakeRedis) -> None:
    """Extract readable article text once and reuse the shared source snapshot."""
    paragraph = (
        "Company earnings increased this quarter as operating costs fell and revenue grew. " * 20
    )
    body = f"<html><body><article><h1>Earnings report</h1><p>{paragraph}</p></article></body></html>".encode()
    pool = Pool([Response(body=body)])
    provider = ArticleProvider(SnapshotCache(redis, "account"), [], pool=pool)
    identifier = await provider.register("https://example.com/story")
    first = await provider.fetch(identifier)
    second = await provider.fetch(identifier)
    assert "Company earnings" in first.snapshot.data["text"]
    assert second.cache_hit
    assert len(pool.visited) == 1


async def test_oversized_and_compressed_articles_rejected(redis: FakeRedis) -> None:
    """Reject payloads that exceed the body limit or ignore identity encoding."""
    for response in [
        Response(body=b"x" * 2_000_001),
        Response(headers=[(b"content-type", b"text/html"), (b"content-encoding", b"gzip")]),
    ]:
        provider = ArticleProvider(SnapshotCache(redis, "account"), [], pool=Pool([response]))
        with pytest.raises(ResearchError):
            await provider._download("https://example.com/story")
