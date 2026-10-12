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


if __name__ == "__main__":
    unittest.main()
