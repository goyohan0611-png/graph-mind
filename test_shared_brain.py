"""Two PCs, one brain: the real MCP tools, two indexes, one shared folder.

What has to hold for it to feel like one memory and not two machines reading each other: a memory
saved on one PC is recalled on the other; a fact corrected on the second PC replaces the first PC's
version everywhere; and nothing but plain log lines is ever written into the shared folder.
"""
from pathlib import Path
import os
import tempfile
import unittest
import unittest.mock

from mcp import Client

from graph_mind_mcp_server import build_server, wait_for_index


def remember(text, **extra):
    return {"content": text, "title": text, "memory_type": "fact", "source_ref": "test",
            "entities": ["사는 곳"], **extra}


class SharedBrainTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_pcs_share_one_brain(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "OneDrive" / "Graph-MIND"
            os.environ["GRAPH_MIND_FOLDER"] = str(shared)
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                    saved = await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    seoul = saved.structured_content["memory_id"]

                os.environ["GRAPH_MIND_DEVICE"] = "laptop"
                async with Client(build_server(root / "laptop-index.sqlite")) as laptop:
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in seen.structured_content["matches"]], [seoul],
                                     "a memory saved on the desktop is recalled on the laptop")
                    moved = await laptop.call_tool("brain_remember", remember(
                        "사는 곳: 부산", supersedes_memory_id=seoul))
                    self.assertFalse(moved.is_error, "the laptop can replace a desktop memory")
                    busan = moved.structured_content["memory_id"]

                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                    now = await desktop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in now.structured_content["matches"]], [busan],
                                     "the correction made on the laptop holds on the desktop")
                    history = await desktop.call_tool("brain_timeline", {"entity": "사는 곳"})
                    entries = history.structured_content["entries"]
                    self.assertEqual([e["memory_id"] for e in entries], [seoul, busan])
                    self.assertTrue(entries[0]["superseded"])

                written = sorted(p.relative_to(shared).as_posix() for p in shared.rglob("*")
                                 if p.is_file())
                self.assertEqual(written, ["log/desktop.jsonl", "log/laptop.jsonl"],
                                 "only plain logs live in the shared folder, never a database")
            finally:
                wait_for_index()          # the index files must be closed before cleanup
                os.environ.pop("GRAPH_MIND_FOLDER", None)
                os.environ.pop("GRAPH_MIND_DEVICE", None)

    async def test_two_pcs_share_one_brain_through_postgres(self):
        """The same, with the log in Postgres (pgserver: a real server, started on a temp dir)."""
        import pgserver
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            server = pgserver.get_server(root / "pg", cleanup_mode="stop")
            os.environ["GRAPH_MIND_FOLDER"] = server.get_uri()
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                    saved = await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    seoul = saved.structured_content["memory_id"]
                os.environ["GRAPH_MIND_DEVICE"] = "laptop"
                async with Client(build_server(root / "laptop-index.sqlite")) as laptop:
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in seen.structured_content["matches"]], [seoul])
                    moved = await laptop.call_tool("brain_remember", remember(
                        "사는 곳: 부산", supersedes_memory_id=seoul))
                    busan = moved.structured_content["memory_id"]
                    shown = await laptop.call_tool("brain_folder", {})
                    self.assertEqual(shown.structured_content["devices"], ["desktop", "laptop"])
                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                    now = await desktop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["memory_id"] for m
                                      in now.structured_content["matches"]], [busan])
            finally:
                wait_for_index()
                os.environ.pop("GRAPH_MIND_FOLDER", None)
                os.environ.pop("GRAPH_MIND_DEVICE", None)
                server.cleanup()

    async def test_connection_code_joins_another_pc(self):
        """'다른 PC도 붙게 해줘' on the desktop, the code pasted on the laptop: one brain. The laptop
        comes in over the network address (not 127.0.0.1), so it is the password that lets it in."""
        import brain_log
        import psycopg
        from pgserver._commands import pg_ctl
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            port, brain_log.PORT = brain_log.PORT, 54391        # never the real hub's port
            brain_log._LOCAL_URI.clear()
            os.environ["GRAPH_MIND_HOME"] = str(root / "home")
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                os.environ["GRAPH_MIND_CONFIG"] = str(root / "desktop-config.json")
                with unittest.mock.patch.object(brain_log, "_firewall", return_value="open"):
                    async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                        shared = await desktop.call_tool("brain_folder", {"path": "share"})
                        await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                code = shared.structured_content["connection_code"]
                self.assertTrue(code.startswith("gm1."))
                self.assertEqual(shared.structured_content["folder"], "this PC's Postgres")
                value = brain_log._decode(code)
                self.assertNotIn("127.0.0.1", value["h"])
                lan = value["h"][0]
                with psycopg.connect(f"postgresql://graphmind:{value['k']}@{lan}:54391/graphmind") as c:
                    self.assertFalse(c.execute("SELECT rolsuper FROM pg_roles "
                                               "WHERE rolname = current_user").fetchone()[0])
                with self.assertRaises(psycopg.OperationalError):
                    psycopg.connect(f"postgresql://graphmind:wrong@{lan}:54391/graphmind")
                with self.assertRaises(psycopg.OperationalError):    # the admin stays local
                    psycopg.connect(f"postgresql://postgres@{lan}:54391/postgres")

                os.environ["GRAPH_MIND_DEVICE"] = "laptop"
                os.environ["GRAPH_MIND_CONFIG"] = str(root / "laptop-config.json")
                async with Client(build_server(root / "laptop-index.sqlite")) as laptop:
                    joined = await laptop.call_tool("brain_folder", {"path": code})
                    self.assertEqual(joined.structured_content["folder"],
                                     f"Postgres on {lan}:54391")
                    self.assertNotIn(value["k"], str(joined.structured_content))
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual([m["content"] for m in seen.structured_content["matches"]],
                                     ["사는 곳: 서울"])
                    self.assertEqual(joined.structured_content["devices"], ["desktop"])
                with self.assertRaises(ValueError):
                    brain_log.set_folder("gm1.not-a-code")
            finally:
                wait_for_index()
                for name in ("GRAPH_MIND_DEVICE", "GRAPH_MIND_CONFIG", "GRAPH_MIND_HOME"):
                    os.environ.pop(name, None)
                if (root / "home" / "postgres" / "postmaster.pid").exists():
                    pg_ctl(["-w", "-m", "fast", "stop"], pgdata=root / "home" / "postgres")
                brain_log.PORT = port
                brain_log._LOCAL_URI.clear()
                brain_log._JOINED.clear()

    async def test_folder_chosen_in_conversation(self):
        """No environment variable: the user names the folder, and that is enough."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shared = root / "GoogleDrive" / "Graph-MIND"
            try:
                os.environ["GRAPH_MIND_DEVICE"] = "desktop"
                os.environ["GRAPH_MIND_CONFIG"] = str(root / "desktop-config.json")
                async with Client(build_server(root / "desktop-index.sqlite")) as desktop:
                    before = await desktop.call_tool("brain_folder", {})
                    self.assertIsNone(before.structured_content["folder"])
                    await desktop.call_tool("brain_remember", remember("사는 곳: 서울"))
                    chosen = await desktop.call_tool("brain_folder", {
                        "path": str(shared), "share_existing": True})
                    self.assertEqual(chosen.structured_content["existing_memories_shared"]
                                     ["memories"], 1, "the memory saved before is now shared")
                    again = await desktop.call_tool("brain_folder", {
                        "path": str(shared), "share_existing": True})
                    self.assertEqual(again.structured_content["existing_memories_shared"]
                                     ["lines_written"], 0, "sharing twice uploads nothing twice")

                os.environ["GRAPH_MIND_DEVICE"] = "laptop"
                os.environ["GRAPH_MIND_CONFIG"] = str(root / "laptop-config.json")
                async with Client(build_server(root / "laptop-index.sqlite")) as laptop:
                    joined = await laptop.call_tool("brain_folder", {"path": str(shared)})
                    self.assertEqual(joined.structured_content["imported_from_other_devices"], 1)
                    seen = await laptop.call_tool("brain_recall", {"query": "사는 곳"})
                    self.assertEqual(len(seen.structured_content["matches"]), 1,
                                     "the laptop knows what the desktop knew")
            finally:
                wait_for_index()
                os.environ.pop("GRAPH_MIND_DEVICE", None)
                os.environ.pop("GRAPH_MIND_CONFIG", None)


if __name__ == "__main__":
    unittest.main()
