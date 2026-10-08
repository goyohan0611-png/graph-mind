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

    def test_passwords_after_their_name(self):
        self.assertEqual(_redact("wifi password: qwer1234! ok")[0], "wifi password: [REDACTED] ok")
        self.assertEqual(_redact("PWD=hunter2")[0], "PWD=[REDACTED]")
        self.assertEqual(_redact("회의실 비밀번호: 7788ab 이야")[0], "회의실 비밀번호: [REDACTED] 이야")
        self.assertEqual(_redact("비번=abcd")[0], "비번=[REDACTED]")
        self.assertEqual(_redact("I forgot my password again")[1], 0)

    def test_unknown_random_tokens(self):
        """A token from a service no pattern knows is still masked by how random it looks;
        what people recall on purpose (commit ids, hashes, UUIDs, code names, model names) is not."""
        for token in ("x7Kq9mP2vL8nR4tY6wZ3aB5c", "tok_Q2w9ErT5yU8iO1pA3sD6fG7h",
                      "Cj0KCQiA2af-BRDzARIsAIVQUOdNiV5qT"):
            self.assertEqual(_redact(f"use {token} here")[0], "use [REDACTED_SECRET] here", token)
        for kept in ("a1e104c9d2f3b4a5c6d7e8f90123456789abcdef",          # commit id
                     "550e8400-e29b-41d4-a716-446655440000",              # UUID
                     "Supermarketdata_exclude_store10_dept_summary",       # code name
                     "paraphrase-multilingual-MiniLM-L12-v2",              # model name
                     "test_connection_code_joins_another_pc2"):
            self.assertEqual(_redact(f"see {kept} now")[1], 0, kept)

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
