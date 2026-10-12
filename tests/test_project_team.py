"""Project brains on a team hub: a teammate sees only the projects given to them, can never read
the owner's personal memory, acts within their role, and loses access when removed."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import importlib.util
import os
import tempfile
import time
import unittest

import brain_log
import project_ledger as pl


class FolderTests(unittest.TestCase):
    def test_a_bound_folder_names_the_default_project(self):
        root = Path(tempfile.mkdtemp())
        (root / "src" / "deep").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"GRAPH_MIND_CONFIG": str(root / "config.json")}):
            pl.bind_folder(root, "graph-mind")
            self.assertEqual(pl.folder_project(root / "src" / "deep"), "graph-mind")
            self.assertIsNone(pl.folder_project(Path(tempfile.mkdtemp())))

    def test_without_team_hubs_only_the_own_ledger_is_used(self):
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"GRAPH_MIND_CONFIG": str(root / "config.json")}):
            found = pl.ledgers()
            self.assertEqual(len(found), 1)
            found[0].create_project("solo", "me")
            found[0].db.close()
            with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
                pl.ledger_for("someone-elses")
            pl.ledger_for("solo").db.close()


@unittest.skipUnless(importlib.util.find_spec("pgserver"), "pgserver is built for Python <3.13")
class TeamHubTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.gettempdir()) / f"graph-mind-team-{os.getpid()}-{time.time_ns()}"
        self.root.mkdir()
        self.saved = (brain_log.PORT, os.environ.get("GRAPH_MIND_HOME"),
                      os.environ.get("GRAPH_MIND_CONFIG"))
        brain_log.PORT = 54393                                   # never the real hub's port
        brain_log._LOCAL_URI.clear()
        os.environ["GRAPH_MIND_HOME"] = str(self.root / "home")
        os.environ["GRAPH_MIND_CONFIG"] = str(self.root / "owner-config.json")
        brain_log._save({"memory_folder": "local", "user_name": "yohan"})

    def tearDown(self):
        from pgserver._commands import pg_ctl
        try:
            pg_ctl(["stop", "-m", "fast"], pgdata=brain_log._pg_data(), timeout=60)
        except Exception:
            pass
        brain_log.PORT = self.saved[0]
        brain_log._LOCAL_URI.clear()
        for name, value in (("GRAPH_MIND_HOME", self.saved[1]), ("GRAPH_MIND_CONFIG", self.saved[2])):
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def test_members_see_their_projects_only_and_never_personal_memory(self):
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.create_project("beta", "yohan")
        decision = owner.publish("alpha", "decision", "허브는 PC 한 대", "yohan")
        owner.publish("beta", "progress", "다른 팀 일", "yohan")
        owner.db.close()
        shared = brain_log.folder()
        brain_log.append(shared, "memory", {"memory_id": "m-personal", "scope": "global",
                                            "memory_type": "fact", "title": "연봉",
                                            "content": "연봉 협상 전략", "effective_at": None,
                                            "known_at": None, "actor": "yohan", "tags": [],
                                            "entities": [], "provenance": {},
                                            "supersedes_memory_id": None})
        with mock.patch.object(brain_log, "_firewall", return_value="open"):
            editor = pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"]
            viewer = pl.invite("jun", "viewer", ["alpha"], "yohan")["team_code"]

        minji = pl._team_ledger(editor)
        self.assertEqual(minji.names(), ["alpha"])                       # beta is invisible
        self.assertEqual(minji.timeline("alpha", by="minji")[0]["summary"], "허브는 PC 한 대")
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            minji.timeline("beta")
        with self.assertRaises(Exception) as denied:                     # personal memory
            minji.db.execute("SELECT line FROM brain_log").fetchall()
        self.assertIn("permission denied", str(denied.exception))
        mine = minji.publish("alpha", "progress", "허브 재시도 추가", "minji")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            minji.retract(decision["id"], "minji")                      # someone else's
        minji.retract(mine["id"], "minji")                              # her own
        minji.db.close()

        jun = pl._team_ledger(viewer)
        self.assertEqual(len(jun.timeline("alpha")), 1)
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.publish("alpha", "progress", "읽기만 가능", "jun")
        jun.db.close()

        pl.remove_member("minji", "yohan")
        with self.assertRaises(Exception):
            pl._team_ledger(editor).names()
        owner = pl.own_ledger()
        actions = [entry["action"] for entry in owner.audit(limit=100)]
        owner.db.close()
        for action in ("invite", "publish", "retract", "timeline", "remove_member"):
            self.assertIn(action, actions)


    def test_a_teammate_picks_up_the_work_through_their_own_assistant(self):
        """A publishes and attaches from their assistant; B, on their own config, joins with the
        code, asks where things stand and opens the file. Everything through brain_checkpoint."""
        import asyncio
        from mcp import Client
        from graph_mind_mcp_server import build_server
        deck = self.root / "제안서.md"
        deck.write_text("# Graph-MIND 제안서\n- 공동 뇌", encoding="utf-8")

        async def run():
            async with Client(build_server(self.root / "a.sqlite")) as a:
                call = lambda **k: a.call_tool("brain_checkpoint", k)  # noqa: E731
                await call(action="create_project", project="graph-mind")
                await call(action="publish", project="graph-mind", type="decision",
                           summary="Postgres 시작 실패 시 최대 10회 재시도",
                           details="brain_log._retry; initdb·pg_ctl start. 남은 일: 원인 확정")
                await call(action="attach", project="graph-mind", path=str(deck),
                           summary="제안서 v1 완료")
                with mock.patch.object(brain_log, "_firewall", return_value="open"):
                    code = (await call(action="invite", member="minji", role="editor",
                                       projects=["graph-mind"])).structured_content["team_code"]
            os.environ["GRAPH_MIND_CONFIG"] = str(self.root / "minji-config.json")
            with mock.patch("project_ledger.open_with_default_app") as opened:
                async with Client(build_server(self.root / "b.sqlite")) as b:
                    call = lambda **k: b.call_tool("brain_checkpoint", k)  # noqa: E731
                    joined = (await call(action="join_team", code=code)).structured_content
                    where = (await call(action="timeline", project="graph-mind")).structured_content
                    shown = (await call(action="open", project="graph-mind",
                                        name="제안서")).structured_content
            return joined, where, shown, opened
        joined, where, shown, opened = asyncio.run(run())
        self.assertEqual(joined["projects"], ["graph-mind"])
        summaries = [c["summary"] for c in where["checkpoints"]]
        self.assertEqual(summaries, ["제안서 v1 완료", "Postgres 시작 실패 시 최대 10회 재시도"])
        self.assertIn("남은 일", where["checkpoints"][1]["details"])
        self.assertEqual(Path(shown["path"]).read_text(encoding="utf-8"), deck.read_text(encoding="utf-8"))
        opened.assert_called_once()

    def test_two_uploads_of_the_same_file_at_once_get_two_versions(self):
        import threading
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        files = []
        for n in range(2):
            path = self.root / f"copy{n}" / "plan.md"
            path.parent.mkdir()
            path.write_text(f"# 계획 {n}", encoding="utf-8")
            files.append(path)
        results = []

        def upload(path):
            book = pl.own_ledger()
            try:
                results.append(book.attach("alpha", path, "계획", "yohan")["artifacts"][0]["version"])
            finally:
                book.db.close()
        threads = [threading.Thread(target=upload, args=(path,)) for path in files]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [1, 2])


if __name__ == "__main__":
    unittest.main()
