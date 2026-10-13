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
            found, unreachable = pl.ledgers()
            self.assertEqual((len(found), unreachable), (1, []))
            found[0].create_project("solo", "me")
            found[0].db.close()
            with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
                pl.ledger_for("someone-elses")
            pl.ledger_for("solo").db.close()


    def test_someone_on_several_team_hubs_says_which_one_gets_a_new_project(self):
        import base64
        import json
        code = lambda host: pl.TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(  # noqa: E731
            {"h": [host], "p": 1, "u": "u", "k": "k", "l": "", "n": "n"}).encode()).decode()
        root = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"GRAPH_MIND_CONFIG": str(root / "config.json")}):
            brain_log._save({"team_hubs": [code("10.0.0.1"), code("10.0.0.2")]})
            with self.assertRaisesRegex(ValueError, "HUB_REQUIRED.*10.0.0.1.*10.0.0.2"):
                pl.home_ledger()


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
        try:
            brain_log.stop_postgres()
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
        import psycopg
        with self.assertRaises(psycopg.OperationalError):               # personal memory: her
            psycopg.connect(pl._team_url(pl._decode_team(editor)).replace(  # login cannot even
                f"/{pl.HUB_DATABASE}", "/graphmind"), connect_timeout=5)    # reach its database
        beta = minji.db.execute("SELECT hub_schema('beta')").fetchone()[0]
        with self.assertRaises(psycopg.Error) as denied:                 # nor another project's
            minji.db.execute(f'SELECT * FROM "{beta}".ledger_checkpoints').fetchall()
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

        pl.remove_account("minji", "yohan")
        with self.assertRaises(Exception):
            pl._team_ledger(editor).names()
        owner = pl.own_ledger()
        actions = [entry["action"] for entry in owner.audit(limit=100)]
        owner.db.close()
        for action in ("invite", "publish", "retract", "timeline"):
            self.assertIn(action, actions)
        self.assertIn("remove_account", [entry["action"] for entry in pl.hub_audit()])

    def test_owners_run_their_projects_and_the_host_only_runs_the_hub(self):
        import contextlib
        import io
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(brain_log, "_firewall", return_value="open"):
            minji_code = pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"]
            jun_code = pl.add_account("jun")["team_code"]
            self.assertIsNone(pl.add_account("jun")["team_code"])     # the code stays valid
        minji, jun = pl._team_ledger(minji_code), pl._team_ledger(jun_code)
        self.assertEqual(jun.names(), [])                     # an account alone opens nothing
        self.assertTrue(minji.create_project("gamma", "minji")["created"])
        with self.assertRaisesRegex(ValueError, "PROJECT_NAME_TAKEN"):
            jun.create_project("gamma", "jun")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            minji.grant("alpha", "jun", "viewer", "minji")    # an editor there, not the owner
        minji.grant("gamma", "jun", "editor", "minji")
        minji.set_instructions("gamma", "테스트 먼저", "minji")
        plan = self.root / "plan.md"
        plan.write_text("# 감마 계획", encoding="utf-8")
        attached = minji.attach("gamma", plan, "계획 v1", "minji")["artifacts"][0]

        host = pl.own_ledger()                                # who runs the hub sees none of it
        self.assertEqual(host.names(), ["alpha"])
        with self.assertRaisesRegex(ValueError, "PROJECT_NOT_FOUND"):
            host.grant("gamma", "yohan", "owner", "yohan")
        with self.assertRaisesRegex(ValueError, "ARTIFACT_NOT_FOUND"):
            host._sql("SELECT hub_read_part(?,?,0)", ("gamma", attached["artifact_id"])).fetchall()
        host.db.close()

        self.assertEqual(jun.names(), ["gamma"])
        self.assertEqual(jun.instructions("gamma"), "테스트 먼저")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.set_instructions("gamma", "바꿔치기", "jun")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            jun.grant("gamma", "yohan", "viewer", "jun")
        note = jun.publish("gamma", "progress", "준 작업", "jun")
        minji.retract(note["id"], "minji")                    # the owner may take it back
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            minji.revoke("gamma", "minji", "minji")
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            minji.grant("gamma", "minji", "editor", "minji")
        with self.assertRaisesRegex(ValueError, "NO_ACCOUNT"):
            minji.grant("gamma", "nobody", "viewer", "minji")
        self.assertEqual({m["member"]: m["role"] for m in jun.members("gamma")},
                         {"minji": "owner", "jun": "editor"})
        self.assertIn("retract", [e["action"] for e in minji.audit(limit=100)])
        minji.db.close()

        users = {a["member"]: a["projects"] for a in pl.accounts()}
        self.assertEqual(users["minji"], {"alpha": "editor", "gamma": "owner"})
        with self.assertRaisesRegex(ValueError, "LAST_OWNER"):
            pl.remove_account("minji")
        pl.remove_account("minji", transfer_to="jun")
        self.assertEqual(jun.members("gamma"), [{"member": "jun", "role": "owner"}])
        jun.db.close()
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            self.assertEqual(pl.hub_main(["users"]), 0)
        self.assertIn('"jun"', printed.getvalue())


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
                    joined = (await call(action="join_team", code=code, name="minji")).structured_content
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

    def test_review_round_trip_between_two_people_and_two_assistants(self):
        """The first demo, end to end: A publishes v1 with its context and asks Minji; she is
        notified, opens it in her own assistant, asks for a change; A is notified, publishes v2
        answering it; she sees what changed since v1 and approves v2; A is notified. Jun, in
        another project, sees none of it; a wrong-version approval and a second identical
        request are refused; with the hub down her assistant says the state is unknown."""
        import asyncio
        from mcp import Client
        from graph_mind_mcp_server import build_server
        owner_config = os.environ["GRAPH_MIND_CONFIG"]
        installer = self.root / "Graph-MIND-Setup.txt"
        installer.write_text("installer 0.6.4\nstep: copy files\n", encoding="utf-8")

        async def as_user(config, db, steps):
            os.environ["GRAPH_MIND_CONFIG"] = config
            async with Client(build_server(self.root / db)) as client:
                call = lambda **k: client.call_tool("brain_checkpoint", k)  # noqa: E731
                return [((await call(**step)).structured_content) for step in steps]

        def run(config, db, *steps):
            return asyncio.run(as_user(config, db, list(steps)))

        with mock.patch.object(brain_log, "_firewall", return_value="open"), \
                mock.patch.object(pl, "toast") as toast, \
                mock.patch("project_ledger.open_with_default_app"):
            run(owner_config, "a.sqlite",
                {"action": "create_project", "project": "graph-mind"},
                {"action": "create_project", "project": "beta"})
            minji_code = run(owner_config, "a.sqlite", {"action": "invite", "member": "minji",
                             "role": "editor", "projects": ["graph-mind"]})[0]["team_code"]
            jun_code = run(owner_config, "a.sqlite", {"action": "invite", "member": "jun",
                           "role": "editor", "projects": ["beta"]})[0]["team_code"]
            up, request, again = run(
                owner_config, "a.sqlite",
                {"action": "attach", "project": "graph-mind", "path": str(installer),
                 "summary": "설치파일 v1", "context": {
                     "what": "설치 단계 정리", "where": "1-2단계", "why": "첫 배포",
                     "verification": "Windows에서 설치 확인", "review_focus": "단계 순서"}},
                {"action": "request_review", "project": "graph-mind", "reviewer": "minji",
                 "name": "Setup"},
                {"action": "request_review", "project": "graph-mind", "reviewer": "minji",
                 "name": "Setup"})
            self.assertEqual(up["artifacts"][0]["version"], 1)
            self.assertTrue(again["duplicate"])

            minji = str(self.root / "minji.json")
            run(minji, "b.sqlite", {"action": "join_team", "code": minji_code, "name": "minji"})
            os.environ["GRAPH_MIND_CONFIG"] = minji
            self.assertEqual([e["key"] for e in pl.notify_new()], [f"{request['id']}:requested"])
            self.assertEqual(pl.notify_new(), [])                     # once only
            opened, refused, asked = run(
                minji, "b.sqlite",
                {"action": "open_review", "review_id": request["id"]},
                {"action": "respond_review", "review_id": request["id"],
                 "decision": "approved", "version": 2},
                {"action": "respond_review", "review_id": request["id"],
                 "decision": "changes_requested", "note": "3단계(바로가기 생성)가 빠졌어요"})
            self.assertEqual(opened["review"]["status"], "in_review")
            self.assertEqual(Path(opened["file"]["path"]).read_text(encoding="utf-8"),
                             installer.read_text(encoding="utf-8"))
            self.assertEqual(opened["context"]["publishers_account"]["review_focus"], "단계 순서")
            self.assertIn("WRONG_VERSION", refused["reason"])
            self.assertEqual(asked["status"], "changes_requested")

            os.environ["GRAPH_MIND_CONFIG"] = owner_config
            self.assertEqual([e["key"] for e in pl.notify_new()],
                             [f"{request['id']}:changes_requested"])
            installer.write_text("installer 0.6.5\nstep: copy files\nstep: create shortcut\n",
                                 encoding="utf-8")
            v2, second = run(
                owner_config, "a.sqlite",
                {"action": "attach", "project": "graph-mind", "path": str(installer),
                 "summary": "설치파일 v2: 바로가기 단계 추가", "context": {
                     "what": "바로가기 생성 단계 추가", "where": "3단계",
                     "addresses": [request["id"]], "verification": "새 PC에서 설치 확인"}},
                {"action": "request_review", "project": "graph-mind", "reviewer": "minji",
                 "name": "Setup", "version": 2})
            self.assertEqual(v2["artifacts"][0]["version"], 2)

            os.environ["GRAPH_MIND_CONFIG"] = minji
            self.assertEqual([e["key"] for e in pl.notify_new()], [f"{second['id']}:requested"])
            opened, approved = run(
                minji, "b.sqlite",
                {"action": "open_review", "review_id": second["id"]},
                {"action": "respond_review", "review_id": second["id"], "decision": "approved",
                 "version": 2, "note": "확인했습니다"})
            self.assertIn("+step: create shortcut", opened["changes"]["changes"]["diff"])
            self.assertEqual(opened["history"]["addresses"][0]["id"], request["id"])
            self.assertEqual(approved["status"], "approved")

            os.environ["GRAPH_MIND_CONFIG"] = owner_config
            self.assertEqual([e["key"] for e in pl.notify_new()], [f"{second['id']}:approved"])

            jun = str(self.root / "jun.json")
            joined, seen, peek = run(jun, "c.sqlite", {"action": "join_team", "code": jun_code, "name": "jun"},
                                     {"action": "reviews"},
                                     {"action": "open_review", "review_id": second["id"]})
            self.assertEqual(joined["projects"], ["beta"])
            self.assertEqual(seen["reviews"], [])
            self.assertEqual(peek["status"], "REFUSED")
            self.assertGreaterEqual(toast.call_count, 4)

            brain_log.stop_postgres()
            down = run(minji, "b.sqlite", {"action": "timeline", "project": "graph-mind"})[0]
            self.assertEqual(down["status"], "UNAVAILABLE")
            self.assertIn("unknown", down["note"])

    def test_the_hub_itself_holds_each_side_of_a_review_to_its_part(self):
        """Straight SQL, past the Python checks: the requester can only cancel an open review,
        the reviewer can only open and decide (and change the decision), nobody changes who or
        which version, and a cancelled review stays closed."""
        import psycopg
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(brain_log, "_firewall", return_value="open"):  # restarts the hub
            minji = pl._team_ledger(pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"])
        owner = pl.own_ledger()
        notes = self.root / "notes.md"
        notes.write_text("# v1", encoding="utf-8")
        owner.attach("alpha", notes, "notes v1", "yohan")
        review = owner.request_review("alpha", "minji", "yohan", name="notes")["id"]
        minji._require("alpha")                                     # her session: alpha's schema
        change = "UPDATE ledger_reviews SET status=%s WHERE id=%s"
        with self.assertRaises(psycopg.Error):                       # requester approving
            owner.db.execute(change, ("approved", review))
        with self.assertRaises(psycopg.Error):                       # reviewer cancelling
            minji.db.execute(change, ("cancelled", review))
        with self.assertRaises(psycopg.Error):                       # another version
            minji.db.execute("UPDATE ledger_reviews SET target='af-0@v9' WHERE id=%s", (review,))
        self.assertEqual(minji.db.execute(change, ("approved", review)).rowcount, 1)
        self.assertEqual(owner.db.execute(change, ("cancelled", review)).rowcount, 0)  # decided
        self.assertEqual(minji.db.execute(change, ("changes_requested", review)).rowcount, 1)
        other = owner.request_review("alpha", "minji", "yohan", name="notes",
                                     note="another look")["id"]
        self.assertEqual(owner.db.execute(change, ("cancelled", other)).rowcount, 1)
        self.assertEqual(minji.db.execute(change, ("approved", other)).rowcount, 0)  # closed
        self.assertEqual(owner.db.execute("SELECT status FROM ledger_reviews WHERE id=%s",
                                          (review,)).fetchone()[0], "changes_requested")
        owner.db.close()
        minji.db.close()

    def test_a_review_request_reaches_the_reviewer_in_real_time(self):
        """The reviewer's PC listens to the hub: the notification comes within seconds of the
        request, not at the next round (set to ten minutes here)."""
        import threading
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(brain_log, "_firewall", return_value="open"):
            code = pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"]
        owner = pl.own_ledger()
        notes = self.root / "notes.md"
        notes.write_text("# v1", encoding="utf-8")
        owner.attach("alpha", notes, "notes v1", "yohan")

        os.environ["GRAPH_MIND_CONFIG"] = str(self.root / "minji.json")
        pl.join_team(code)
        shown, stop = threading.Event(), threading.Event()
        with mock.patch.object(pl, "toast", side_effect=lambda *a: shown.set() or True) as toast:
            watcher = threading.Thread(target=pl.watch_inbox, args=(stop, 600), daemon=True)
            watcher.start()
            time.sleep(8)                                     # listening, first round done
            self.assertFalse(shown.is_set())
            started = time.monotonic()
            owner.request_review("alpha", "minji", "yohan", name="notes")
            self.assertTrue(shown.wait(10))
            self.assertLess(time.monotonic() - started, 10)
            stop.set()
        self.assertIn("검토 요청", toast.call_args[0][0])
        owner.db.close()

    def test_people_choose_the_names_others_see(self):
        """Joining with a name, renaming everywhere at once, one name per person per hub, and
        owners finding people by the name they chose."""
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.create_project("beta", "yohan")
        owner.db.close()
        with mock.patch.object(brain_log, "_firewall", return_value="open"):
            minji = pl.invite("mini", "editor", ["alpha"], "yohan")["team_code"]
            jun = pl.add_account("jun")["team_code"]
        owner_config = os.environ["GRAPH_MIND_CONFIG"]

        os.environ["GRAPH_MIND_CONFIG"] = str(self.root / "minji.json")
        self.assertEqual(pl.join_team(minji, "김민지")["joined_as"], "김민지")
        self.assertEqual(brain_log._config()["user_name"], "김민지")
        os.environ["GRAPH_MIND_CONFIG"] = str(self.root / "jun.json")
        with self.assertRaisesRegex(ValueError, "NAME_TAKEN"):
            pl.join_team(jun, "김민지")
        self.assertEqual(brain_log._config().get("team_hubs", []), [])   # not half joined

        os.environ["GRAPH_MIND_CONFIG"] = owner_config
        owner = pl.own_ledger()
        owner.grant("beta", "김민지", "viewer", "yohan")                  # by her chosen name
        self.assertEqual({m["member"]: m["role"] for m in owner.members("beta")},
                         {"yohan": "owner", "김민지": "viewer"})
        owner.db.close()
        self.assertEqual(pl.set_display_name("고요한")["renamed_on"], ["your project hub"])
        owner = pl.own_ledger()
        self.assertEqual(owner.by, "고요한")
        self.assertIn({"member": "고요한", "role": "owner"}, owner.members("alpha"))
        owner.db.close()

    def test_projects_from_one_shared_set_of_tables_become_brains_of_their_own(self):
        """A hub from before: every project in one set of tables beside personal memory, all
        sealed with one hub key. Each moves into its own schema under its own key, file and
        history included, and reads back the same."""
        import hashlib
        import json
        brain_log._save({"share_password": "pw-before"})
        old_key = pl._fernet(pl.ledger_secret("pw-before"))
        brain_log.local_postgres()
        content = "옛 허브의 파일\n".encode("utf-8")
        location = f"{pl.hub_artifacts().as_posix()}/{hashlib.md5(b'legacy').hexdigest()}" \
                   "__af-0123456789ab__v1"
        Path(location + "__p0000.bin").write_bytes(old_key.encrypt(content))
        artifact = {"kind": "file", "artifact_id": "af-0123456789ab", "name": "a.txt",
                    "version": 1, "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content), "parts": 1, "location": location, "created_by": "yohan"}
        checkpoint = {"id": "cp-0123456789ab", "project": "legacy", "type": "artifact",
                      "summary": "옛 결과물", "details": "", "actor": "yohan",
                      "created_at": "2026-10-01T10:00:00+09:00", "artifacts": [artifact],
                      "source_refs": [], "supersedes": None, "context": {}}
        with pl._admin("graphmind") as old:
            old.execute("CREATE TABLE ledger_projects(name TEXT PRIMARY KEY, created_by TEXT, "
                        "created_at TEXT, instructions TEXT NOT NULL DEFAULT '')")
            old.execute("CREATE TABLE ledger_checkpoints(id TEXT PRIMARY KEY, project TEXT, "
                        "stamp TEXT, created_at TEXT, status TEXT, actor_role TEXT, payload TEXT)")
            old.execute("CREATE TABLE ledger_artifacts(artifact_id TEXT PRIMARY KEY, project TEXT, "
                        "version INT, parts INT, size BIGINT, location TEXT, status TEXT, "
                        "actor_role TEXT, created_at TEXT)")
            old.execute("CREATE TABLE ledger_access(member_role TEXT, project TEXT, role TEXT, "
                        "member TEXT)")
            old.execute("CREATE TABLE ledger_audit(stamp TEXT, at TEXT, actor_role TEXT, "
                        "actor TEXT, action TEXT, target TEXT)")
            old.execute("INSERT INTO ledger_projects VALUES('legacy', 'yohan', '2026-10-01', '')")
            old.execute("INSERT INTO ledger_checkpoints VALUES(%s, 'legacy', '1', %s, "
                        "'published', 'graphmind', %s)",
                        (checkpoint["id"], checkpoint["created_at"],
                         old_key.encrypt(json.dumps(checkpoint).encode()).decode()))
            old.execute("INSERT INTO ledger_artifacts VALUES('af-0123456789ab', 'legacy', 1, 1, "
                        "%s, %s, 'published', 'graphmind', '2026-10-01')", (len(content), location))
            old.execute("INSERT INTO ledger_access VALUES('graphmind', 'legacy', 'owner', 'yohan')")
            old.execute("INSERT INTO ledger_audit VALUES('1', 'x', 'graphmind', 'yohan', "
                        "'publish', 'legacy/cp-0123456789ab')")
        pl._PREPARED.clear()
        pl.prepare_hub()

        book = pl.own_ledger()
        self.assertEqual(book.timeline("legacy")[0]["summary"], "옛 결과물")
        opened = book.fetch("legacy", name="a.txt", into=self.root / "out")
        self.assertEqual(Path(opened["path"]).read_bytes(), content)
        self.assertEqual(book.members("legacy"), [{"member": "yohan", "role": "owner"}])
        schema = book.db.execute("SELECT hub_schema('legacy')").fetchone()[0]
        book.db.close()
        self.assertFalse(Path(location + "__p0000.bin").exists())          # moved, not copied
        with pl._admin() as hub:                                           # under its own key
            stored = hub.execute(f'SELECT payload FROM "{schema}".ledger_checkpoints').fetchone()[0]
        with self.assertRaises(Exception):
            old_key.decrypt(stored.encode())
        with pl._admin("graphmind") as old:
            self.assertIsNotNone(old.execute(
                "SELECT to_regclass('public.old_ledger_projects')").fetchone()[0])

    def test_a_thread_on_the_hub_and_the_operators_space_tools(self):
        """Any member comments (a viewer too, with a file); only the author edits; the operator
        sees sizes per project and removes a project whole."""
        owner = pl.own_ledger()
        owner.create_project("alpha", "yohan")
        owner.db.close()
        with mock.patch.object(brain_log, "_firewall", return_value="open"):
            minji = pl._team_ledger(pl.invite("minji", "editor", ["alpha"], "yohan")["team_code"])
            jun = pl._team_ledger(pl.invite("jun", "viewer", ["alpha"], "yohan")["team_code"])
        owner = pl.own_ledger()
        notes = self.root / "notes.md"
        notes.write_text("# v1", encoding="utf-8")
        owner.attach("alpha", notes, "notes v1", "yohan")
        review = owner.request_review("alpha", "minji", "yohan", name="notes")["id"]
        said = minji.comment(review, "2번 문단 확인", "minji")
        looked = jun.comment(review, "스크린샷 첨부", "jun", parent_id=said["id"],
                             files=[("shot.png", b"PNG" + bytes(range(200)))])
        self.assertEqual(looked["attachments"][0]["name"], "shot.png")
        with self.assertRaisesRegex(ValueError, "NOT_ALLOWED"):
            owner.edit_comment(said["id"], "바꿔치기", "yohan")
        minji.edit_comment(said["id"], "2번 문단 숫자 확인", "minji")
        thread = owner.comments(review)
        self.assertEqual([c["text"] for c in thread], ["2번 문단 숫자 확인", "스크린샷 첨부"])
        got = owner.attachment(review, looked["attachments"][0]["artifact_id"], self.root / "s.png")
        self.assertEqual(Path(got["path"]).read_bytes(), b"PNG" + bytes(range(200)))
        for book in (owner, minji, jun):
            book.db.close()

        used = pl.hub_usage()
        self.assertEqual([(p["project"], p["files"]) for p in used["projects"]], [("alpha", 2)])
        self.assertGreater(used["disk"]["free"], 0)
        with self.assertRaisesRegex(ValueError, "CONFIRM"):
            pl.delete_project("alpha", confirm="alph")
        self.assertGreater(pl.delete_project("alpha", confirm="alpha")["freed_bytes"], 0)
        owner = pl.own_ledger()
        self.assertEqual(owner.names(), [])
        owner.db.close()

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
        results, errors = [], []

        def upload(path):
            book = pl.own_ledger()
            try:
                results.append(book.attach("alpha", path, "계획", "yohan")["artifacts"][0]["version"])
            except Exception as error:
                cause = error.__cause__ or error.__context__
                errors.append(f"{type(error).__name__}: {error} <- {cause!r}"[:600])
            finally:
                book.db.close()
        threads = [threading.Thread(target=upload, args=(path,)) for path in files]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), [1, 2], errors)


if __name__ == "__main__":
    unittest.main()
