from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from typing import Any, Protocol

type SocketOption = tuple[int, int, int | bytes | bytearray] | tuple[int, int, None, int]


class ArticleResponse(Protocol):
    """Expose the response fields consumed by bounded article downloads."""

    status: int
    headers: list[tuple[bytes, bytes]]

    def aiter_stream(self) -> AsyncIterator[bytes]:
        """Iterate over response-body chunks without buffering the full article."""
        ...


class ArticlePool(Protocol):
    """Allow production and test transports to provide the article HTTP interface."""

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        extensions: dict[str, Any],
    ) -> AbstractAsyncContextManager[ArticleResponse]:
        """Open a response stream with caller-specified headers and timeout extensions."""
        ...

    async def aclose(self) -> None:
        """Release connections held by the transport."""
        ...
