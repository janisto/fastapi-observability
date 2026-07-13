from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx2


@asynccontextmanager
async def asgi_client(
    app: Any,
    *,
    raise_app_exceptions: bool = True,
    follow_redirects: bool = True,
    client: tuple[str, int] = ("127.0.0.1", 123),
) -> AsyncIterator[httpx2.AsyncClient]:
    """Create the repository's HTTPX2-only in-process ASGI test client."""
    transport = httpx2.ASGITransport(
        app=app,
        raise_app_exceptions=raise_app_exceptions,
        client=client,
    )
    async with httpx2.AsyncClient(
        transport=transport,
        base_url="http://testserver",
        follow_redirects=follow_redirects,
    ) as test_client:
        yield test_client
