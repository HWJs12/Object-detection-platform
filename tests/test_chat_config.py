import asyncio
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.core import llm
from config import Settings


class ChatConfigTests(unittest.TestCase):
    def test_project_key_takes_precedence_over_legacy_environment(self):
        settings = Settings(llm_enabled=True, llm_api_key="file-key")
        with patch.dict(os.environ, {"LLM_API_KEY": "old-key", "UWD_LLM_API_KEY": " "}):
            self.assertEqual(settings.llm_api_key_effective, "file-key")
            self.assertEqual(settings.to_dict()["llm_api_key"], "***")
            with patch.dict(os.environ, {"UWD_LLM_API_KEY": " project-key "}):
                self.assertEqual(settings.llm_api_key_effective, "project-key")
            settings.llm_api_key = ""
            self.assertEqual(settings.llm_api_key_effective, "old-key")
            self.assertTrue(settings.llm_ready)


class ChatAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_auth_error_omits_upstream_body_from_reply_and_logs(self):
        def reject(request):
            self.assertEqual(request.headers["Authorization"], "Bearer project-key")
            return httpx.Response(401, text="echoed-secret")

        async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as client:
            with patch.dict(os.environ, {"UWD_LLM_API_KEY": "project-key"}):
                with patch.object(llm.settings, "llm_enabled", True), \
                     patch.object(llm, "build_system_prompt", return_value="Help with platform usage"), \
                     patch.object(llm, "_get_client", return_value=client), \
                     patch.object(llm, "_get_semaphore", return_value=asyncio.Semaphore(1)), \
                     self.assertLogs(llm.logger, level="WARNING") as logs:
                    events = [
                        json.loads(event.removeprefix("data: "))
                        async for event in llm.stream_chat([{"role": "user", "content": "Help"}])
                    ]

        self.assertEqual([event["type"] for event in events], ["error"])
        self.assertIn("HTTP 401", events[0]["message"])
        self.assertNotIn("echoed-secret", str(events))
        self.assertNotIn("echoed-secret", "\n".join(logs.output))
