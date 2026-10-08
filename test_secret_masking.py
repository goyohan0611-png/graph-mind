"""Secrets never reach the store: not from captured chat, and not from a proactive brain_remember."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from conversation_memory import _redact
from graph_mind_mcp_server import build_server, wait_for_index

KEYS = {
    "sk-proj-" + "a" * 40: "[REDACTED_OPENAI_KEY]",
    "ghp_" + "b" * 36: "[REDACTED_GITHUB_TOKEN]",
    "AKIA" + "C" * 16: "[REDACTED_AWS_KEY]",
    "AIza" + "d" * 35: "[REDACTED_GOOGLE_KEY]",
    "xoxb-" + "1234567890-abc": "[REDACTED_SLACK_TOKEN]",
}


class SecretMaskingTests(unittest.IsolatedAsyncioTestCase):
    def test_patterns(self):
        for secret, mask in KEYS.items():
            self.assertEqual(_redact(f"my key {secret} ok")[0], f"my key {mask} ok")
        self.assertEqual(_redact("postgresql://graph:hunter2@100.64.1.2:5432/brain")[0],
                         "postgresql://graph:[REDACTED]@100.64.1.2:5432/brain")
        self.assertEqual(_redact("김치볶음밥 먹었어, postgres://me@localhost/db")[1], 0)

    async def test_brain_remember_masks_what_it_saves(self):
        secret = "sk-proj-" + "z" * 40
        with tempfile.TemporaryDirectory() as directory:
            try:
                async with Client(build_server(Path(directory) / "brain.sqlite")) as client:
                    await client.call_tool("brain_remember", {
                        "content": f"내 OpenAI 키는 {secret} 이고 DB는 postgres://u:pw1234@h/db",
                        "source_ref": "test"})
                    found = await client.call_tool("brain_recall", {"query": "OpenAI 키"})
                    text = str(found.structured_content)
            finally:
                wait_for_index()
        self.assertNotIn(secret, text)
        self.assertNotIn("pw1234", text)
        self.assertIn("[REDACTED_OPENAI_KEY]", text)


if __name__ == "__main__":
    unittest.main()
