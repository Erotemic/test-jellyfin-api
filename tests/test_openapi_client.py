import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from jellyfin_apiclient_python.openapi.client import AsyncJellyfinOpenAPIClient, JellyfinOpenAPIClient


class TestAsyncJellyfinOpenAPIClient(unittest.IsolatedAsyncioTestCase):
    async def test_build_url_normalizes_slashes(self):
        client = AsyncJellyfinOpenAPIClient("https://example.com/api/")
        self.assertEqual(client.build_url("Users"), "https://example.com/api/Users")
        self.assertEqual(client.build_url("/Items/1"), "https://example.com/api/Items/1")

    async def test_request_uses_session_and_headers(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.__aexit__ = AsyncMock(return_value=False)
        response.headers = {"Content-Type": "application/json"}
        response.json = AsyncMock(return_value={"ok": True})
        response.raise_for_status = MagicMock()

        session = AsyncMock()
        session.request.return_value = response

        client = AsyncJellyfinOpenAPIClient(
            "https://example.com/api", auth_token="token123", user_id="user-1", session=session
        )
        data = await client.request("GET", "/Items")

        self.assertEqual(data, {"ok": True})
        session.request.assert_awaited_with(
            "GET",
            "https://example.com/api/Items",
            headers={
                "X-Emby-Token": "token123",
                'X-Emby-Authorization': 'MediaBrowser Client="jellyfin-apiclient-python", Device="user-1"',
            },
            timeout=30,
        )

    async def test_request_returns_text_for_non_json(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.__aexit__ = AsyncMock(return_value=False)
        response.headers = {"Content-Type": "text/plain"}
        response.text = AsyncMock(return_value="pong")
        response.raise_for_status = MagicMock()

        session = AsyncMock()
        session.request.return_value = response

        client = AsyncJellyfinOpenAPIClient("https://example.com/api", session=session)
        data = await client.request("GET", "/Ping")

        self.assertEqual(data, "pong")
        session.request.assert_awaited()


class TestJellyfinOpenAPIClient(unittest.TestCase):
    def test_sync_wrapper_runs_async_request(self):
        client = JellyfinOpenAPIClient("https://example.com")
        with patch.object(client.async_client, "request", new=AsyncMock(return_value={"result": True})) as mock_request:
            result = client.request("GET", "/Items/1")
        self.assertEqual(result, {"result": True})
        mock_request.assert_awaited_with("GET", "/Items/1")
        client.close()

    def test_sync_context_manager(self):
        with patch("jellyfin_apiclient_python.openapi.client.AsyncJellyfinOpenAPIClient.close", new=AsyncMock()) as mock_close:
            with JellyfinOpenAPIClient("https://example.com") as client:
                self.assertIsInstance(client.async_client, AsyncJellyfinOpenAPIClient)
        mock_close.assert_awaited()
