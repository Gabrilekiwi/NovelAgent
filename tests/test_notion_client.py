from __future__ import annotations

import os
import unittest
from urllib import error
from unittest.mock import patch

from api.notion_client import NotionClientError, _urllib_transport, create_database_page, query_database_pages
from core.state.memory import load_notion_memory_context


class NotionClientTest(unittest.TestCase):
    def test_query_database_pages_paginates_with_transport(self) -> None:
        calls: list[dict] = []

        def transport(url, headers, body):
            calls.append(body)
            if "start_cursor" not in body:
                return {
                    "results": [{"id": "page-1", "properties": {"Type": "location", "Name": "shelter"}}],
                    "has_more": True,
                    "next_cursor": "cursor-2",
                }
            return {
                "results": [{"id": "page-2", "properties": {"Type": "character", "Name": "Mira"}}],
                "has_more": False,
            }

        pages = query_database_pages(database_id="db", api_key="secret", transport=transport)

        self.assertEqual(["page-1", "page-2"], [page["id"] for page in pages])
        self.assertEqual("cursor-2", calls[1]["start_cursor"])

    def test_query_database_pages_requires_config(self) -> None:
        with self.assertRaises(NotionClientError):
            query_database_pages(database_id="", api_key="", transport=lambda *_: {})

    def test_load_notion_memory_context_normalizes_pages(self) -> None:
        def transport(url, headers, body):
            return {
                "results": [
                    {
                        "id": "page-1",
                        "url": "https://notion.test/page-1",
                        "properties": {
                            "Type": "location",
                            "Name": "shelter",
                            "Risk": "rising",
                        },
                    }
                ],
                "has_more": False,
            }

        memory = load_notion_memory_context(database_id="db", api_key="secret", transport=transport)

        self.assertEqual("notion-api", memory["source"])
        self.assertEqual("location", memory["items"][0]["type"])
        self.assertEqual("shelter", memory["items"][0]["name"])
        self.assertEqual("rising", memory["items"][0]["data"]["risk"])
        self.assertEqual("https://notion.test/page-1", memory["source_mappings"][0]["page_url"])

    def test_create_database_page_uses_transport(self) -> None:
        calls: list[dict] = []

        def transport(url, headers, body):
            calls.append({"url": url, "headers": headers, "body": body})
            return {"id": "created-page"}

        result = create_database_page(
            database_id="db",
            api_key="secret",
            properties={"Name": {"title": [{"text": {"content": "Memory"}}]}},
            transport=transport,
        )

        self.assertEqual("created-page", result["id"])
        self.assertTrue(calls[0]["url"].endswith("/pages"))
        self.assertEqual({"database_id": "db"}, calls[0]["body"]["parent"])

    def test_urllib_transport_uses_configured_timeout(self) -> None:
        original_timeout = os.environ.get("NOTION_TIMEOUT_SECONDS")
        os.environ["NOTION_TIMEOUT_SECONDS"] = "6"

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self):
                return b'{"ok": true}'

        try:
            with patch("api.notion_client.request.urlopen", return_value=Response()) as urlopen:
                self.assertEqual({"ok": True}, _urllib_transport("https://notion.test", {}, {"x": 1}))
        finally:
            if original_timeout is None:
                os.environ.pop("NOTION_TIMEOUT_SECONDS", None)
            else:
                os.environ["NOTION_TIMEOUT_SECONDS"] = original_timeout

        self.assertEqual(6, urlopen.call_args.kwargs["timeout"])

    def test_urllib_transport_falls_back_across_resolved_ips(self) -> None:
        original_timeout = os.environ.get("NOTION_TIMEOUT_SECONDS")
        os.environ["NOTION_TIMEOUT_SECONDS"] = "6"
        attempts: list[str] = []

        def fake_request(**kwargs):
            attempts.append(kwargs["connect_host"])
            if kwargs["connect_host"] == "208.103.161.1":
                raise ConnectionResetError("reset")
            return {"ok": True}

        try:
            with patch("api.notion_client.request.urlopen", side_effect=error.URLError("reset")), patch(
                "api.notion_client.socket.getaddrinfo",
                return_value=[
                    (0, 0, 0, "", ("208.103.161.1", 443)),
                    (0, 0, 0, "", ("208.103.161.2", 443)),
                ],
            ), patch("api.notion_client._https_json_request", side_effect=fake_request):
                result = _urllib_transport("https://api.notion.com/v1/databases/db/query", {}, {"x": 1})
        finally:
            if original_timeout is None:
                os.environ.pop("NOTION_TIMEOUT_SECONDS", None)
            else:
                os.environ["NOTION_TIMEOUT_SECONDS"] = original_timeout

        self.assertEqual({"ok": True}, result)
        self.assertEqual(["208.103.161.1", "208.103.161.2"], attempts)


if __name__ == "__main__":
    unittest.main()
