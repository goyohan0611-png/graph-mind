"""install.py on a fresh, fake home: every app found gets the memory server, a second run (or a
run after moving the folder) leaves exactly one entry, and the user's other settings survive."""
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
            environment = {"USERPROFILE": str(home), "HOME": str(home),
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
            self.assertTrue((home / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu"
                             / "Programs" / "Startup" / "Graph-MIND Automatic Capture.lnk").exists())
            self.assertTrue((home / ".graph-mind" / "automatic-capture.json").exists())
            chosen = json.loads((home / ".graph-mind" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(chosen["memory_folder"], code)

    def test_mangled_code_stops_before_anything_changes(self):
        with self.assertRaises(ValueError):
            install.main(["--skip-packages", "--join", "gm1.%%%"])


if __name__ == "__main__":
    unittest.main()
