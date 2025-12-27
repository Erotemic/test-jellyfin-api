"""Async-first client for the Jellyfin OpenAPI surface.

The module exposes two entry points:

* :class:`AsyncJellyfinOpenAPIClient` for asyncio-native workflows.
* :class:`JellyfinOpenAPIClient` for ergonomic synchronous access that wraps the
  async client.

Both clients focus on composable request helpers rather than mirroring the full
OpenAPI schema inside this repository. The expectation is that generated code
can consume :meth:`AsyncJellyfinOpenAPIClient.request` for transport.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Dict, Optional

try:
    import aiohttp  # type: ignore
except ImportError:  # pragma: no cover - exercised when aiohttp is unavailable
    from . import _aiohttp_stub as aiohttp


class AsyncJellyfinOpenAPIClient:
    """Async transport helper for the Jellyfin OpenAPI surface.

    Parameters
    ----------
    base_url:
        The server root (for example ``\"https://demo.jellyfin.org\"``). Trailing
        slashes are ignored.
    auth_token:
        Optional access token added to the request headers.
    user_id:
        Optional user identifier appended to the authorization header.
    session:
        Existing :class:`aiohttp.ClientSession` to reuse. When omitted, a new
        session is created and managed by the client.
    timeout:
        Per-request timeout in seconds (default: ``30``).

    Examples
    --------
    Normalize paths and build URLs:

    >>> client = AsyncJellyfinOpenAPIClient("https://example.com/api")
    >>> client.build_url("Users")
    'https://example.com/api/Users'
    >>> client.build_url("/Users/1")
    'https://example.com/api/Users/1'
    """

    def __init__(
        self,
        base_url: str,
        *,
        auth_token: Optional[str] = None,
        user_id: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
        timeout: int = 30,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._session = session
        self._owns_session = session is None
        self._auth_token = auth_token
        self._user_id = user_id
        self._timeout = timeout

    def build_url(self, path: str) -> str:
        """Return an absolute URL by joining ``path`` with ``base_url``."""
        normalized = path.lstrip("/")
        return f"{self.base_url}/{normalized}"

    @property
    def default_headers(self) -> Dict[str, str]:
        """Default headers applied to every request."""
        headers: Dict[str, str] = {}
        if self._auth_token:
            headers["X-Emby-Token"] = self._auth_token
        if self._user_id:
            headers[
                "X-Emby-Authorization"
            ] = f'MediaBrowser Client="jellyfin-apiclient-python", Device="{self._user_id}"'
        return headers

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None:
            self._session = aiohttp.ClientSession()
            self._owns_session = True
        return self._session

    async def close(self) -> None:
        """Close the underlying session if we created it."""
        if self._session and self._owns_session:
            await self._session.close()
            self._session = None

    async def request(
        self, method: str, path: str, *, headers: Optional[Dict[str, str]] = None, **kwargs: Any
    ) -> Any:
        """Perform an HTTP request against the Jellyfin API.

        The returned value is JSON-decoded when the ``Content-Type`` header
        includes ``application/json``; otherwise the response text is returned.
        """
        session = await self._get_session()
        url = self.build_url(path)
        merged_headers = {**self.default_headers, **(headers or {})}
        request_ctx = session.request(method, url, headers=merged_headers, timeout=self._timeout, **kwargs)
        if inspect.isawaitable(request_ctx):
            request_ctx = await request_ctx
        async with request_ctx as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "")
            if "application/json" in content_type:
                return await response.json()
            return await response.text()

    async def __aenter__(self) -> "AsyncJellyfinOpenAPIClient":
        await self._get_session()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self.close()


class JellyfinOpenAPIClient:
    """Synchronous wrapper for :class:`AsyncJellyfinOpenAPIClient`.

    Examples
    --------
    The wrapper provides a blocking ``request`` method that proxies to the
    async client:

    >>> client = JellyfinOpenAPIClient(\"https://example.com/api\", auth_token=\"token\")
    >>> isinstance(client.async_client, AsyncJellyfinOpenAPIClient)
    True
    """

    def __init__(self, base_url: str, **kwargs: Any) -> None:
        self._loop = asyncio.new_event_loop()
        self.async_client = AsyncJellyfinOpenAPIClient(base_url, **kwargs)

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Synchronously perform a request using the async client."""
        return self._loop.run_until_complete(self.async_client.request(method, path, **kwargs))

    def close(self) -> None:
        """Synchronously close the underlying async client and loop."""
        self._loop.run_until_complete(self.async_client.close())
        self._loop.close()

    def __enter__(self) -> "JellyfinOpenAPIClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()
