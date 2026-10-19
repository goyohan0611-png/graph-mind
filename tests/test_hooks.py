"""Gemini CLI: its hook payloads (as documented) are saved through graph_mind.hook and stored
once; the installer adds our hooks next to the user's own and takes only ours away again.
Copilot CLI and Cursor: their own transcripts are read, in the shape they were seen to write."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import io
import json
import os
import tempfile
import unittest

from graph_mind import hook, install
from graph_mind.conversation_memory import (ConversationMemoryStore, CopilotConversationCapture,
                                            CursorConversationCapture, HookConversationCapture)


def run(agent, event, payload):
    out = io.StringIO()
    out.reconfigure = lambda **_: None
    stdin = io.StringIO(json.dumps(payload))
    stdin.reconfigure = lambda **_: None
    with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out):
        hook.main([agent, event])
    return json.loads(out.getvalue())


class HookTests(unittest.TestCase):
    def test_gemini_cli_is_saved_once(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": directory}):
            run("gemini-cli", "BeforeAgent", {"session_id": "g1", "cwd": directory,
                                              "hook_event_name": "BeforeAgent",
                                              "prompt": "our golden retriever Kongi turned three"})
            for _ in range(2):                     # fired again for the same reply: kept once
                run("gemini-cli", "AfterAgent", {"session_id": "g1", "cwd": directory,
                                                 "prompt": "x",
                                                 "prompt_response": "Happy birthday!"})
            self.assertEqual(run("gemini-cli", "AfterAgent", {"prompt_response": ""}), {})
            rows = []
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                for folder in sorted(hook.hooks_root().iterdir()):
                    capture = HookConversationCapture(
                        folder.name, folder, store, state_path=Path(directory) / folder.name)
                    capture.initialize_baseline()
                    capture.scan()
                    capture.scan()                               # nothing new: nothing twice
                rows = store.db.execute("SELECT client, role, content_redacted FROM "
                                        "conversation_turns ORDER BY client, happened_at")
                rows = sorted(tuple(r) for r in rows.fetchall())
        self.assertEqual(rows, sorted([
            ("gemini-cli", "user", "our golden retriever Kongi turned three"),
            ("gemini-cli", "assistant", "Happy birthday!")]))

    def test_the_conversation_about_deleting_is_not_kept(self):
        """docs/privacy.md: asking to delete a secret must not store the secret again. Gemini's
        hooks carried only text, so the forget call was invisible and the request was kept."""
        # a second apart, as separate hook processes are; within one millisecond, lines tied
        # on their time and the order (and so what the forget swept) was left to chance
        seconds = iter(range(100))
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": directory}), \
                mock.patch.object(hook, "_now",
                                  lambda: f"2026-10-19T00:00:{next(seconds):02d}.000000Z"):
            def say(event, **fields):
                run("gemini-cli", event, {"session_id": "g3", **fields})
            say("BeforeAgent", prompt="my dog is called Kongi")
            say("AfterAgent", prompt_response="Nice name!")
            say("BeforeAgent", prompt="delete the wifi password qwerty123 I typed earlier")
            say("AfterTool", tool_name="mcp_graph-mind-memory_brain_view")     # not a forget
            say("AfterTool", tool_name="mcp_graph-mind-memory_brain_forget")
            say("AfterAgent", prompt_response="Deleted it.")
            say("BeforeAgent", prompt="thanks, what's my dog called?")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = HookConversationCapture(
                    "gemini-cli", hook.hooks_root() / "gemini-cli", store,
                    state_path=Path(directory) / "c.json")       # the real erase
                capture.initialize_baseline()
                capture.scan()
                kept = [r[0] for r in store.db.execute(
                    "SELECT content_redacted FROM conversation_turns ORDER BY happened_at")]
        self.assertEqual(kept, ["my dog is called Kongi", "Nice name!",
                                "thanks, what's my dog called?"])

    def test_cursor_transcript_is_read_as_said(self):
        """As the Cursor CLI agent wrote it: the typed text inside <user_query>, the reply's
        text blocks; a brain_forget call through CallDynamicTool drops the exchange."""
        with tempfile.TemporaryDirectory() as directory:
            chat = Path(directory) / "projects" / "C-work" / "agent-transcripts" / "t1"
            chat.mkdir(parents=True)
            def say(role, *blocks):
                return {"role": role, "message": {"content": list(blocks)}}
            records = [
                say("user", {"type": "text",
                             "text": "<timestamp>Sunday, Oct 18, 2026, 2:46 PM (UTC+9)</timestamp>"
                             "\n<user_query>\nI moved to Busan\n</user_query>"}),
                say("assistant", {"type": "text", "text": "Saving that."},
                    {"type": "tool_use", "name": "CallDynamicTool",
                     "input": {"namespace": "graph-mind-memory", "toolName": "brain_remember"}}),
                say("assistant", {"type": "text", "text": "Busan is lovely."}),
                {"type": "turn_ended", "status": "success"}]
            (chat / "t1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records),
                                           encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = CursorConversationCapture(Path(directory) / "projects", store,
                                                    state_path=Path(directory) / "c.json")
                capture._write_state({})
                capture.scan()
                capture.scan()
                rows = store.db.execute("SELECT client, client_session_id, role, "
                                        "content_redacted FROM conversation_turns "
                                        "ORDER BY rowid").fetchall()
                said = {r[0] for r in store.db.execute(
                    "SELECT happened_at FROM conversation_turns")}
                self.assertFalse(capture._forget_call(records[1]))
                self.assertTrue(capture._forget_call(say("assistant", {
                    "type": "tool_use", "name": "CallDynamicTool",
                    "input": {"toolName": "brain_forget"}})))
        self.assertEqual([tuple(r) for r in rows],
                         [("cursor", "t1", "user", "I moved to Busan"),
                          ("cursor", "t1", "assistant", "Saving that."),
                          ("cursor", "t1", "assistant", "Busan is lovely.")])
        # when it was said, from the user's <timestamp>, and the replies with it (not scan time)
        self.assertEqual(len(said), 1)
        self.assertTrue(next(iter(said)).startswith("2026-10-18"), said)

    def test_hook_command_survives_a_space_and_a_shadowing_project(self):
        """Gemini CLI runs hooks through PowerShell on Windows, where a quoted path alone is a
        string; and apps start them in the user's project, where a graph_mind.py shadowed us."""
        python = r"C:\Users\Jane Doe\py\python.exe"
        with mock.patch.object(install.sys, "executable", python), \
                mock.patch.object(install.os, "name", "nt"):
            self.assertEqual(install._hook_command("gemini-cli", "BeforeAgent"),
                             f'& "{python}" -m graph_mind.hook gemini-cli BeforeAgent')
        entry = install._hook_files(Path("x"))[0][3]["BeforeAgent"]["hooks"][0]
        self.assertEqual(entry["env"]["PYTHONSAFEPATH"], "1")
        self.assertEqual(install.ENV["PYTHONSAFEPATH"], "1")

    def test_apps_saved_for_them_are_told_so(self):
        """Their conversations are captured, so the model is not asked to save them again."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in (".gemini", ".copilot", ".lmstudio", ".cursor"):
                (home / name).mkdir()
            environment = {"USERPROFILE": directory, "HOME": directory,
                           "APPDATA": str(home / "AppData")}
            with mock.patch.dict(os.environ, environment):
                install.agent_hooks(home)            # Gemini counts as saved only once hooked
                install.other_apps(home)
            def env(*parts):
                return json.loads(home.joinpath(*parts).read_text(encoding="utf-8"))[
                    "mcpServers"]["graph-mind-memory"]["env"]
            self.assertEqual(env(".gemini", "settings.json").get("GRAPH_MIND_CAPTURED"), "1")
            self.assertEqual(env(".copilot", "mcp-config.json").get("GRAPH_MIND_CAPTURED"), "1")
            self.assertNotIn("GRAPH_MIND_CAPTURED", env(".lmstudio", "mcp.json"))
            self.assertNotIn("GRAPH_MIND_CAPTURED", env(".cursor", "mcp.json"))   # editor: unseen
            # Gemini's hooks break (an unexpected "hooks" value): nothing saves its chats now,
            # so the flag that told the model not to save them goes, even if set before
            settings = home / ".gemini" / "settings.json"
            broken = json.loads(settings.read_text(encoding="utf-8"))
            broken["hooks"] = ["not", "an", "object"]
            settings.write_text(json.dumps(broken), encoding="utf-8")
            with mock.patch.dict(os.environ, environment):
                self.assertEqual(install.agent_hooks(home), [])
                install.other_apps(home)
            self.assertNotIn("GRAPH_MIND_CAPTURED", env(".gemini", "settings.json"))

    def test_copilot_cli_session_is_read_as_said(self):
        """Records as Copilot CLI 1.0.98 writes them: what was typed (not transformedContent),
        the reply's text, nothing from hooks or a reply that only calls a tool."""
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / "state" / "s1"
            session.mkdir(parents=True)
            at = "2026-10-18T05:38:11.000Z"
            records = [
                {"type": "session.start", "id": "e0", "timestamp": at,
                 "data": {"sessionId": "s1", "context": {"cwd": directory}}},
                {"type": "hook.start", "id": "e1", "timestamp": at,
                 "data": {"input": {"prompt": "my sister is Mina"}}},
                {"type": "user.message", "id": "e2", "timestamp": at,
                 "data": {"content": "my sister is Mina",
                          "transformedContent": "<current_datetime>...</current_datetime>"}},
                {"type": "assistant.message", "id": "e3", "timestamp": at,
                 "data": {"content": "", "toolRequests": [{"name": "view"}]}},
                {"type": "assistant.message", "id": "e4", "timestamp": at,
                 "data": {"content": "Nice to know about Mina.", "toolRequests": []}}]
            (session / "events.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
            with ConversationMemoryStore(Path(directory) / "m.sqlite") as store:
                capture = CopilotConversationCapture(Path(directory) / "state", store,
                                                     state_path=Path(directory) / "c.json")
                capture._write_state({})                  # read from the start
                capture.scan()
                capture.scan()
                rows = store.db.execute("SELECT client, client_session_id, role, "
                                        "content_redacted FROM conversation_turns "
                                        "ORDER BY rowid").fetchall()
        self.assertEqual([tuple(r) for r in rows],
                         [("copilot-cli", "s1", "user", "my sister is Mina"),
                          ("copilot-cli", "s1", "assistant", "Nice to know about Mina.")])

    def test_gemini_gets_the_memory_before_it_answers(self):
        with mock.patch("graph_mind.inject.context", return_value="Graph-MIND memory: Kongi"):
            answer = run("gemini-cli", "BeforeAgent", {"session_id": "g2", "prompt": "my dog?"})
        self.assertEqual(answer["hookSpecificOutput"]["additionalContext"],
                         "Graph-MIND memory: Kongi")

    def test_installer_adds_ours_beside_the_users_and_removes_only_ours(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".gemini").mkdir()
            theirs = {"hooks": [{"type": "command", "command": "C:/their/hook.exe"}]}
            (home / ".gemini" / "settings.json").write_text(json.dumps(
                {"theme": "Dracula", "hooks": {"BeforeAgent": [theirs], "enabled": True}}),
                encoding="utf-8")                # "enabled" stopped the whole install once
            for _ in range(2):                                   # again: still one of ours
                self.assertEqual(install.agent_hooks(home), ["Gemini CLI"])

            def read():
                return json.loads((home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
            gemini = read()
            self.assertEqual(gemini["theme"], "Dracula")
            self.assertEqual(gemini["hooks"]["BeforeAgent"][0], theirs)
            self.assertEqual(len(gemini["hooks"]["BeforeAgent"]), 2)
            self.assertIn("graph_mind.hook gemini-cli AfterAgent",
                          gemini["hooks"]["AfterAgent"][0]["hooks"][0]["command"])
            install.agent_hooks(home, remove=True)
            self.assertEqual(read()["hooks"], {"BeforeAgent": [theirs], "enabled": True})


if __name__ == "__main__":
    unittest.main()
