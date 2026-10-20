import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from graph_mind.conversation_memory import ConversationMemoryStore
from graph_mind.local_brain import LocalBrainStore
from graph_mind.mcp_server import build_server, wait_for_index


class TransplantTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_whole_memory_moves_to_another_pc_as_one_file_facts_included(self):
        with tempfile.TemporaryDirectory() as directory:
            old, new = Path(directory) / "old.sqlite", Path(directory) / "new.sqlite"
            brain = Path(directory) / "brain.jsonl"
            with ConversationMemoryStore(old) as turns:
                turns.record({"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                              "scope": "global", "role": "user",
                              "content_redacted": "콩이를 제주도에 데려갔어", "raw_sha256": "x",
                              "source_path": "p", "happened_at": "2026-03-01T09:00:00",
                              "known_at": "2026-03-01T09:00:00", "source_ordinal": 1})
            async with Client(build_server(old, terminal=True)) as client:
                for when, text in [("2024-03-01", "I live in Busan"),
                                   ("2026-09-20", "We moved to Seoul, Mapo")]:
                    await client.call_tool("brain_remember", {
                        "content": text, "about": "me", "attribute": "home_city",
                        "effective_at": when + "T09:00:00"})
                out = (await client.call_tool("brain_folder", {"export_to": str(brain)})
                       ).structured_content["exported"]
            async with Client(build_server(new, terminal=True)) as client:
                first = (await client.call_tool("brain_folder", {"import_from": str(brain)})
                         ).structured_content["imported"]
                again = (await client.call_tool("brain_folder", {"import_from": str(brain)})
                         ).structured_content["imported"]
            wait_for_index()
            with LocalBrainStore(new) as store:
                homes = [m for m in store.recall("me", as_of="2099-01-01T00:00:00")["matches"]
                         if m.get("earlier") is not None]
            with ConversationMemoryStore(new) as turns:
                found = turns.search("콩이 제주도")["turns"]
        self.assertEqual((out["memories"], out["turns"]), (2, 1))
        self.assertEqual(first["imported"], 3)
        self.assertEqual(again["imported"], 0)                     # nothing twice
        self.assertEqual([m["content"] for m in homes], ["We moved to Seoul, Mapo"])  # still a fact
        self.assertEqual(len(found), 1)

    async def test_a_chat_cannot_move_or_export_the_memory(self):
        # a web page the model reads could ask for any of these; only the user's terminal does them
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            target = Path(directory) / "out.jsonl"
            async with Client(build_server(Path(directory) / "m.sqlite")) as client:
                answers = [(await client.call_tool("brain_folder", call)).structured_content
                           for call in ({"path": "share"}, {"path": "gm1.abc"},
                                        {"path": str(directory)}, {"share_existing": True},
                                        {"export_to": str(target)})]
        self.assertEqual({a["status"] for a in answers}, {"RUN_IN_TERMINAL"})
        self.assertEqual(answers[1]["command"], 'graph-mind folder "gm1.abc"')
        self.assertEqual(answers[4]["command"], f'graph-mind export "{target}"')
        self.assertFalse(target.exists())


class MeaningTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_memory_is_found_by_meaning_not_only_by_its_words(self):
        # OpenCode has no capture: what the model saved is all there is, and "what breed is my
        # dog?" shares no word with it (two of three OpenCode models answered "not in memory")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            async with Client(build_server(Path(directory) / "m.sqlite")) as client:
                await client.call_tool("brain_remember", {
                    "content": "Our golden retriever Kongi turned three last week, about 30 kg."})
                await client.call_tool("brain_remember", {"content": "We use Postgres."})
                found = {}
                for query in ("What breed is my dog?", "우리 강아지 품종이 뭐였지?",
                              "Paris weather forecast?"):
                    rows = (await client.call_tool("brain_recall", {"query": query})
                            ).structured_content.get("matches", [])
                    found[query] = [m["content"][:10] for m in rows]
        self.assertEqual(found["What breed is my dog?"][:1], ["Our golden"])
        self.assertEqual(found["우리 강아지 품종이 뭐였지?"][:1], ["Our golden"])
        self.assertEqual(found["Paris weather forecast?"], [])


if __name__ == "__main__":
    unittest.main()
