"""Gemini CLI, Cursor, Copilot CLI: their hook payloads (as documented) are saved through
graph_mind.hook and stored once under each agent's name; the installer adds our hooks next to
the user's own and takes only ours away again."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import io
import json
import os
import tempfile
import unittest

from graph_mind import hook, install
from graph_mind.conversation_memory import ConversationMemoryStore, HookConversationCapture


def run(agent, event, payload):
    out = io.StringIO()
    out.reconfigure = lambda **_: None
    stdin = io.StringIO(json.dumps(payload))
    stdin.reconfigure = lambda **_: None
    with mock.patch("sys.stdin", stdin), mock.patch("sys.stdout", out):
        hook.main([agent, event])
    return json.loads(out.getvalue())


class HookTests(unittest.TestCase):
    def test_each_agent_is_saved_under_its_own_name(self):
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": directory}):
            run("gemini-cli", "BeforeAgent", {"session_id": "g1", "cwd": directory,
                                              "hook_event_name": "BeforeAgent",
                                              "prompt": "our golden retriever Kongi turned three"})
            run("gemini-cli", "AfterAgent", {"session_id": "g1", "cwd": directory,
                                             "prompt": "x", "prompt_response": "Happy birthday!"})
            self.assertEqual(run("cursor", "beforeSubmitPrompt", {
                "conversation_id": "c1", "workspace_roots": [directory],
                "prompt": "I moved to Busan"}), {"continue": True})
            run("cursor", "afterAgentResponse", {"conversation_id": "c1", "text": "Nice!"})
            run("copilot-cli", "userPromptSubmitted", {"sessionId": "p1", "cwd": directory,
                                                       "prompt": "my sister is Mina"})
            self.assertEqual(run("cursor", "afterAgentResponse", {"text": ""}), {})
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
            ("gemini-cli", "assistant", "Happy birthday!"),
            ("cursor", "user", "I moved to Busan"), ("cursor", "assistant", "Nice!"),
            ("copilot-cli", "user", "my sister is Mina")]))

    def test_gemini_gets_the_memory_before_it_answers(self):
        with mock.patch("graph_mind.inject.context", return_value="Graph-MIND memory: Kongi"):
            answer = run("gemini-cli", "BeforeAgent", {"session_id": "g2", "prompt": "my dog?"})
        self.assertEqual(answer["hookSpecificOutput"]["additionalContext"],
                         "Graph-MIND memory: Kongi")

    def test_installer_adds_ours_beside_the_users_and_removes_only_ours(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            for name in (".gemini", ".cursor", ".copilot"):
                (home / name).mkdir()
            theirs = {"command": "C:/their/hook.exe", "timeout": 10}
            (home / ".cursor" / "hooks.json").write_text(json.dumps(
                {"version": 1, "hooks": {"beforeSubmitPrompt": [theirs]}}), encoding="utf-8")
            (home / ".gemini" / "settings.json").write_text(json.dumps({"theme": "Dracula"}),
                                                            encoding="utf-8")
            for _ in range(2):                                   # again: still one of ours
                self.assertEqual(install.agent_hooks(home),
                                 ["Gemini CLI", "Cursor", "Copilot CLI"])

            def read(*parts):
                return json.loads(home.joinpath(*parts).read_text(encoding="utf-8"))
            cursor = read(".cursor", "hooks.json")["hooks"]
            self.assertEqual(cursor["beforeSubmitPrompt"][0], theirs)
            self.assertEqual(len(cursor["beforeSubmitPrompt"]), 2)
            self.assertIn("graph_mind.hook cursor afterAgentResponse",
                          cursor["afterAgentResponse"][0]["command"])
            gemini = read(".gemini", "settings.json")
            self.assertEqual(gemini["theme"], "Dracula")
            self.assertEqual(len(gemini["hooks"]["BeforeAgent"]), 1)
            self.assertIn("userPromptSubmitted",
                          read(".copilot", "hooks", "graph-mind.json")["hooks"])
            install.agent_hooks(home, remove=True)
            self.assertEqual(read(".cursor", "hooks.json")["hooks"],
                             {"beforeSubmitPrompt": [theirs]})
            self.assertEqual(read(".gemini", "settings.json")["hooks"], {})
            self.assertFalse((home / ".copilot" / "hooks" / "graph-mind.json").exists())


if __name__ == "__main__":
    unittest.main()
