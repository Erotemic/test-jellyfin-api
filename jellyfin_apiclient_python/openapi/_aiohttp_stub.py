"""Lightweight aiohttp compatibility shim.

The real aiohttp package is preferred. This shim mirrors the very small subset
of the interface required by :mod:`jellyfin_apiclient_python.openapi.client`
and delegates network operations to :mod:`requests`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

import requests


class ClientResponse:
    def __init__(self, response: requests.Response) -> None:
        self._response = response
        self.status = response.status_code
        self.headers = dict(response.headers)

    def raise_for_status(self) -> None:
        self._response.raise_for_status()

    async def json(self) -> Any:
        return self._response.json()

    async def text(self) -> str:
        return self._response.text

    async def __aenter__(self) -> "ClientResponse":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        return None


class ClientSession:
    """Minimal async wrapper around :mod:`requests`."""

    def __init__(self) -> None:
        self._closed = False

    async def request(
        self, method: str, url: str, *, headers: Optional[Dict[str, str]] = None, timeout: Optional[int] = None, **kwargs: Any
    ) -> ClientResponse:
        loop = asyncio.get_running_loop()
        response: requests.Response = await loop.run_in_executor(
            None, lambda: requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        )
        return ClientResponse(response)

    async def close(self) -> None:
        self._closed = True
