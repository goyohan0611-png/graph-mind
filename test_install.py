"""install.py on a fresh, fake home: every app found gets the memory server, a second run (or a
run after moving the folder) leaves exactly one entry, and the user's other settings survive."""
import _isolate  # noqa: F401  (first: never touch the real ~/.graph-mind)
from pathlib import Path
from unittest import mock
import base64
import json
import os
import sys
import tempfile
import unittest

import install


class InstallTests(unittest.TestCase):
    def test_registers_every_app_once_and_joins(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".codex").mkdir()
            (home / ".codex" / "config.toml").write_text(
                "model = 'x'\n\n[mcp_servers.graph-mind-memory]\ncommand = 'old'\n"
                "args = ['old.py']\n\n[mcp_servers.graph-mind-memory.env]\nA = '1'\n\n"
                "[mcp_servers.other]\nurl = 'u'\n", encoding="utf-8")
            (home / ".claude.json").write_text(json.dumps({"keep": 1}), encoding="utf-8")
            desktop = home / "AppData" / "Roaming" / "Claude"
            desktop.mkdir(parents=True)
            (desktop / "claude_desktop_config.json").write_text(json.dumps({"mcpServers": {
                "graph-mind-memory": {"command": "old", "env": {"MINE": "1"}}}}), encoding="utf-8")
            code = "gm1." + base64.urlsafe_b64encode(json.dumps(
                {"h": ["192.0.2.1"], "p": 54329, "k": "pw"}).encode()).decode()
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "GRAPH_MIND_HOME": str(home / ".graph-mind"),
                           "GRAPH_MIND_CONFIG": str(home / ".graph-mind" / "config.json")}
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("install.shutil.which", return_value=None):
                for _ in range(2):
                    install.main(["--skip-packages", "--skip-model", "--no-start",
                                  "--join", code])
            toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
            self.assertEqual(toml.count("[mcp_servers.graph-mind-memory]"), 1)
            self.assertIn(str(install.SERVER), toml)
            self.assertIn("[mcp_servers.other]", toml)
            self.assertNotIn("'old'", toml)
            code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
            self.assertEqual(code_cli["keep"], 1)
            self.assertEqual(code_cli["mcpServers"]["graph-mind-memory"]["command"], sys.executable)
            app = json.loads((desktop / "claude_desktop_config.json").read_text(encoding="utf-8"))
            entry = app["mcpServers"]["graph-mind-memory"]
            self.assertEqual(entry["args"], [str(install.SERVER)])
            self.assertEqual(entry["env"]["MINE"], "1")
            self.assertNotIn("type", entry)
            login_item = {
                "win32": home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
                         / "Programs" / "Startup" / "Graph-MIND Automatic Capture.lnk",
                "darwin": home / "Library" / "LaunchAgents" / "com.graph-mind.capture.plist",
            }.get(sys.platform, home / ".config" / "autostart" / "graph-mind-capture.desktop")
            self.assertTrue(login_item.exists(), login_item)
            capture = json.loads((home / ".graph-mind" / "automatic-capture.json")
                                 .read_text(encoding="utf-8"))
            self.assertEqual(capture["code_workspaces"], [])    # never site-packages
            chosen = json.loads((home / ".graph-mind" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(chosen["memory_folder"], code)

            # and back out: every app unregistered, the user's other settings untouched,
            # the memory kept unless asked to purge it
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("install.shutil.which", return_value=None):
                install.uninstall([])
                toml = (home / ".codex" / "config.toml").read_text(encoding="utf-8")
                self.assertNotIn("graph-mind-memory", toml)
                self.assertIn("[mcp_servers.other]", toml)
                self.assertIn("model = 'x'", toml)
                code_cli = json.loads((home / ".claude.json").read_text(encoding="utf-8"))
                self.assertNotIn("graph-mind-memory", code_cli["mcpServers"])
                self.assertEqual(code_cli["keep"], 1)
                app = json.loads((desktop / "claude_desktop_config.json").read_text(encoding="utf-8"))
                self.assertNotIn("graph-mind-memory", app["mcpServers"])
                self.assertFalse(login_item.exists())
                self.assertTrue((home / ".graph-mind").exists(), "memory kept by default")
                install.uninstall(["--purge", "--yes"])
                self.assertFalse((home / ".graph-mind").exists())

    def test_every_installed_mcp_client_is_connected_and_disconnected(self):
        """Cursor, Windsurf, Gemini CLI, LM Studio, VS Code and Cline: each gets the server in
        its own format when its folder exists, keeps its other settings, and loses only our
        entry on uninstall. A VS Code file with comments (not JSON) is left alone."""
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            environment = {"USERPROFILE": str(home), "HOME": str(home), "XDG_CONFIG_HOME": "",
                           "APPDATA": str(home / "AppData" / "Roaming"),
                           "GRAPH_MIND_HOME": str(home / ".graph-mind"),
                           "GRAPH_MIND_CONFIG": str(home / ".graph-mind" / "config.json")}
            with mock.patch.dict(os.environ, environment):
                vscode = install._vscode_user_dir(home)
            (home / ".cursor").mkdir()
            (home / ".cursor" / "mcp.json").write_text(json.dumps(
                {"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
            (home / ".codeium" / "windsurf").mkdir(parents=True)
            (home / ".gemini").mkdir()
            (home / ".gemini" / "settings.json").write_text(json.dumps({"theme": "Dracula"}),
                                                            encoding="utf-8")
            (home / ".lmstudio").mkdir()
            (vscode / "globalStorage" / "saoudrizwan.claude-dev").mkdir(parents=True)
            (vscode / "mcp.json").write_text(json.dumps({"servers": {"other": {"command": "y"}}}),
                                             encoding="utf-8")
            with mock.patch.dict(os.environ, environment), \
                    mock.patch("install.shutil.which", return_value=None):
                connected = install.other_apps(home)
                self.assertEqual(sorted(connected), sorted(
                    ["Cursor", "Windsurf", "Gemini CLI", "LM Studio", "VS Code", "Cline"]))
                read = lambda p: json.loads(p.read_text(encoding="utf-8"))
                self.assertIn("other", read(home / ".cursor" / "mcp.json")["mcpServers"])
                self.assertIn("graph-mind-memory", read(home / ".cursor" / "mcp.json")["mcpServers"])
                self.assertEqual(read(home / ".gemini" / "settings.json")["theme"], "Dracula")
                vs = read(vscode / "mcp.json")["servers"]
                self.assertEqual(vs["graph-mind-memory"]["type"], "stdio")
                self.assertIn("other", vs)
                install.uninstall([])
                self.assertNotIn("graph-mind-memory", read(home / ".cursor" / "mcp.json")["mcpServers"])
                self.assertNotIn("graph-mind-memory", read(vscode / "mcp.json")["servers"])
                self.assertEqual(read(home / ".gemini" / "settings.json")["theme"], "Dracula")
                (vscode / "mcp.json").write_text('{\n  // my servers\n  "servers": {}\n}',
                                                 encoding="utf-8")
                self.assertNotIn("VS Code", install.other_apps(home))
                self.assertIn("// my servers", (vscode / "mcp.json").read_text(encoding="utf-8"))

    def test_mangled_code_stops_before_anything_changes(self):
        with self.assertRaises(ValueError):
            install.main(["--skip-packages", "--join", "gm1.%%%"])


    def test_model_goes_to_own_folder_when_the_usual_cache_is_locked(self):
        """A fresh Windows profile refused ~/.cache (access denied): Hugging Face's whole home
        moves to ~/.graph-mind/huggingface, and every later process is pointed there."""
        import local_embedder

        def locked(*_):
            raise PermissionError(5, "Access is denied: ~/.cache/huggingface")
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.dict(os.environ, {"GRAPH_MIND_HOME": directory}), \
                mock.patch.object(local_embedder, "model_paths", locked), \
                mock.patch("install.subprocess.run") as retry:
            os.environ.pop("HF_HOME", None)
            install.embedding_model()
            own = str(Path(directory) / "huggingface")
            self.assertEqual(retry.call_args.kwargs["env"]["HF_HOME"], own)
            import local_embedder
            local_embedder.use_own_hf_home()          # what the server does when it starts
            self.assertEqual(os.environ["HF_HOME"], own)


if __name__ == "__main__":
    unittest.main()
