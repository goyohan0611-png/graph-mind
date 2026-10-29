"""One line on a new PC: plug Turnbook into every AI app found there.

    turnbook-install                     # this PC keeps its own brain
    turnbook-install --join gm1.XXXX     # ...and joins the brain another PC shared

It registers the memory server with every MCP client it finds (Claude Code, Claude Desktop,
Codex, OpenCode, Cursor, Windsurf, Gemini CLI, Copilot CLI, VS Code, Cline, LM Studio), adds
hooks that save the conversation where an app has them, starts the capture
service at login, and downloads the local embedding model so the first question is not the one
that waits for it. From a source checkout it first installs the checkout itself (pip install
-e). Running it again is safe: it re-points every app at this Python.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
CHECKOUT = HERE.parent.parent         # a source checkout (pyproject.toml), else site-packages
if __package__ in (None, ""):                # run as a file from a checkout: import it from there
    sys.path.insert(0, str(HERE.parent))


def module(name):
    """`python -m name`, minus the current folder on Python 3.10, which ignores PYTHONSAFEPATH:
    apps start us inside the user's project, and a graph_mind/ there would run instead."""
    if sys.version_info >= (3, 11):
        return ["-m", name]
    return ["-c", "import sys; sys.path[:] = [p for p in sys.path if p]; import runpy; "
                  f"runpy.run_module('{name}', run_name='__main__')"]


SERVER = module("graph_mind.mcp_server")
CAPTURE = ["-m", "graph_mind.capture_cli", "run"]
NAME = "graph-mind-memory"
# PYTHONSAFEPATH (3.11+): apps start the server in the user's project folder, and `python -m`
# looks there first; a graph_mind.py or graph_mind/ in that project replaced the real package
ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONSAFEPATH": "1"}
# apps whose transcripts the capture service reads: every turn is saved without the model,
# so the model need not spend a tool call on brain_remember for ordinary chat
CAPTURED = {**ENV, "GRAPH_MIND_CAPTURED": "1"}


# what one app's settings can throw at us (not JSON, an unexpected shape, a file the app holds
# open): that app is skipped, never the whole install
APP_ERRORS = (ValueError, TypeError, AttributeError, OSError)


def step(message):
    print("-", message, flush=True)


def packages():
    done = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                           "-q", "-e", str(CHECKOUT)])
    if done.returncode:
        # PyTorch ships files nested deep enough to pass Windows' 260-character path limit when
        # Python lives under a long folder: pip then fails halfway with "No such file".
        sys.exit("\nPackage install failed. On Windows the usual cause is the 260-character path "
                 "limit:\n  enable long paths (run as administrator, then restart):\n"
                 "    reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
                 "/v LongPathsEnabled /t REG_DWORD /d 1 /f\n"
                 "  or install Python or this folder under a shorter path, e.g. C:\\graph-mind")


def _json_entry(path: Path, key: str = "mcpServers", env=ENV, **extra) -> None:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = settings.setdefault(key, {})
    environment = {**servers.get(NAME, {}).get("env", {}), **env}   # keep the user's own settings
    if "GRAPH_MIND_CAPTURED" not in env:     # ours to set: a stale one told the model not to save
        environment.pop("GRAPH_MIND_CAPTURED", None)
    servers[NAME] = {**extra, "command": sys.executable, "args": SERVER, "env": environment}
    temporary = path.with_suffix(".graph-mind.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def claude_code(home: Path) -> str:
    claude = shutil.which("claude")      # full path: an npm install is claude.CMD, which
    if claude:                           # Windows will not launch by its bare name
        environment = [flag for key, value in CAPTURED.items() for flag in ("-e", f"{key}={value}")]
        try:
            subprocess.run([claude, "mcp", "remove", "-s", "user", NAME], capture_output=True)
            # the name before -e: the CLI's -e takes every value that follows, the name included
            subprocess.run([claude, "mcp", "add", "-s", "user", NAME, *environment, "--",
                            sys.executable, *SERVER], check=True, capture_output=True)
            return "Claude Code"
        except (OSError, subprocess.CalledProcessError):
            pass      # Windows refused to launch it (access denied, seen live): write the file
    if (home / ".claude.json").exists():
        _json_entry(home / ".claude.json", env=CAPTURED, type="stdio")
        return "Claude Code"
    return ""


def _desktop_roots(home: Path) -> list[Path]:
    roots = [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude",
             home / "Library" / "Application Support" / "Claude"]
    packages_dir = home / "AppData" / "Local" / "Packages"
    if packages_dir.is_dir():               # the Microsoft Store build keeps its own copy
        roots += [p / "LocalCache" / "Roaming" / "Claude" for p in packages_dir.glob("Claude_*")]
    return roots


def claude_desktop(home: Path) -> list[str]:
    done = []
    for root in _desktop_roots(home):
        if root.is_dir():
            _json_entry(root / "claude_desktop_config.json")
            done.append("Claude Desktop")
    return done[:1]


def _vscode_user_dir(home: Path) -> Path:
    if platform.system() == "Windows":
        return Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Code" / "User"
    if platform.system() == "Darwin":
        return home / "Library" / "Application Support" / "Code" / "User"
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "Code" / "User"


def _other_apps(home: Path) -> list:
    """(name, config file, servers key, extra fields) for MCP clients beyond Claude and Codex.
    An app counts as installed when its own folder exists; its config file is created if not."""
    vscode = _vscode_user_dir(home)
    return [
        ("Cursor", home / ".cursor", home / ".cursor" / "mcp.json", "mcpServers", {}),
        ("Windsurf", home / ".codeium" / "windsurf",
         home / ".codeium" / "windsurf" / "mcp_config.json", "mcpServers", {}),
        ("Gemini CLI", home / ".gemini", home / ".gemini" / "settings.json", "mcpServers", {}),
        ("Copilot CLI", home / ".copilot", home / ".copilot" / "mcp-config.json", "mcpServers",
         {"type": "local", "tools": ["*"]}),
        ("LM Studio", home / ".lmstudio", home / ".lmstudio" / "mcp.json", "mcpServers", {}),
        ("VS Code", vscode, vscode / "mcp.json", "servers", {"type": "stdio"}),
        ("Cline", vscode / "globalStorage" / "saoudrizwan.claude-dev",
         vscode / "globalStorage" / "saoudrizwan.claude-dev" / "settings"
         / "cline_mcp_settings.json",
         "mcpServers", {}),
    ]


# their conversations are saved without the model (transcripts or hooks): the model is not told
# to call brain_remember for ordinary chat, which stored the same thing twice. Not Cursor: its
# transcript was seen from the CLI agent only, and its config is shared with the editor, whose
# chats would then be neither captured nor remembered (a double save is the lesser harm)
SAVED_FOR_THEM = {"Gemini CLI", "Copilot CLI"}
HOOKED = {"Gemini CLI"}                    # saved only while our hooks are in its settings


def _has_our_hooks(home: Path, app: str) -> bool:
    """Whether app's hooks file holds Turnbook's hooks: a failed hook install must not leave
    the model told its conversations are saved, when nothing saves them."""
    for name, _, path, _ in _hook_files(home):
        if name == app:
            try:
                return _ours(json.loads(path.read_text(encoding="utf-8")).get("hooks"))
            except APP_ERRORS:
                return False
    return False


def other_apps(home: Path) -> list[str]:
    done = []
    for name, folder, config, key, extra in _other_apps(home):
        if not folder.is_dir():
            continue
        try:
            config.parent.mkdir(parents=True, exist_ok=True)
            saved = name in SAVED_FOR_THEM and (name not in HOOKED or _has_our_hooks(home, name))
            _json_entry(config, key, env=CAPTURED if saved else ENV, **extra)
            done.append(name)
        except APP_ERRORS:
            # a settings file with comments (VS Code allows them) is not JSON, or the app holds
            # the file open: leave it alone, and carry on with the other apps
            step(f"{name}: could not edit {config}; add the server there by hand")
    return done


def _hook_command(agent: str, event: str) -> str:
    """Gemini CLI runs a hook through PowerShell on Windows and sh elsewhere. In PowerShell a
    quoted path is a string, not a command, so it needs the call operator; a profile folder with
    a space ("C:\\Users\\Jane Doe") made the plain quoted form fail without a word."""
    arguments = [*module("graph_mind.hook"), agent, event]
    if os.name == "nt":                # single quotes: literal in PowerShell, '' escapes one
        rest = " ".join(a if a.replace("-", "").replace(".", "").replace("_", "").isalnum()
                        else "'" + a.replace("'", "''") + "'" for a in arguments)
        # the path single-quoted too: in double quotes PowerShell expands "$Doe" and runs "$()"
        return "& '" + sys.executable.replace("'", "''") + "' " + rest
    return f"{shlex.quote(sys.executable)} {shlex.join(arguments)}"


def _plain_command(agent: str, event: str) -> str:
    """A command line every shell runs the same (cmd, PowerShell, bash): Claude Code's and
    Codex's hooks do not say which shell they use on Windows. No quotes, so no spaces: the
    short 8.3 form of a path that has them."""
    python = sys.executable
    if os.name == "nt" and " " in python:
        import ctypes
        buffer = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(python, buffer, 1024):
            python = buffer.value
    # -P: not the user's project folder first (PYTHONSAFEPATH; these hooks pass no env).
    # ponytail: 3.10 has no -P, so a graph_mind/ in the project could shadow ours there
    arguments = ["-P"] * (sys.version_info >= (3, 11)) + ["-m", "graph_mind.hook"]
    if os.name == "nt":
        return " ".join([python.replace("\\", "/"), *arguments, agent, event])
    return shlex.join([python, *arguments, agent, event])


def _ours(entry) -> bool:
    return "graph_mind.hook" in json.dumps(entry)


def _hook_files(home: Path) -> list:
    """(app, folder, hooks file, {event: entry}) for agents whose hooks save the conversation."""
    gemini = {event: {"matcher": matcher, "hooks": [{
        "type": "command", "name": "graph-mind", "timeout": 20000, "env": ENV,
        "command": _hook_command("gemini-cli", event)}]}
        # AfterTool only for brain_forget: the conversation about deleting is not kept
        for event, matcher in (("BeforeAgent", "*"), ("AfterAgent", "*"),
                               ("AfterTool", "brain_forget"))}
    # the others' conversations are read from their transcripts: these hooks only add the
    # memory for each message (graph_mind.hook.INJECT_ONLY)
    def prompt(agent):
        return {"UserPromptSubmit": {"hooks": [{
            "type": "command", "timeout": 20,
            "command": _plain_command(agent, "UserPromptSubmit")}]}}
    shell = "powershell" if os.name == "nt" else "bash"
    copilot = {"userPromptTransformed": {
        "type": "command", "timeoutSec": 20, "env": ENV,
        shell: _hook_command("copilot-cli", "userPromptTransformed")}}
    copilot_home = Path(os.environ.get("COPILOT_HOME") or home / ".copilot")
    return [("Gemini CLI", home / ".gemini", home / ".gemini" / "settings.json", gemini),
            ("Claude Code", home / ".claude", home / ".claude" / "settings.json",
             prompt("claude-code")),
            ("Codex", home / ".codex", home / ".codex" / "hooks.json", prompt("codex")),
            ("Copilot CLI", copilot_home, copilot_home / "hooks" / "turnbook.json", copilot)]


def _write_hooks(path: Path, entries: dict) -> None:
    """Our entry for each event, in place of an older one of ours; everyone else's kept."""
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if path.parent.name == "hooks":     # Copilot CLI's own hooks file: it wants a version
        settings.setdefault("version", 1)
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks is not an object")
    for event in list(hooks):
        if not isinstance(hooks[event], list):    # Gemini CLI keeps "enabled", "disabled" here too
            continue
        hooks[event] = [entry for entry in hooks[event] if not _ours(entry)]
        if not hooks[event]:
            del hooks[event]
    for event, entry in entries.items():
        hooks.setdefault(event, []).append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".graph-mind.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def agent_hooks(home: Path, remove: bool = False) -> list[str]:
    """Gemini CLI keeps no transcript we read: its hooks hand each
    message to graph_mind.hook, which saves it for the capture service (and, in Gemini CLI,
    adds the memory before the model answers)."""
    done = []
    for name, folder, path, entries in _hook_files(home):
        if not folder.is_dir() or (remove and not path.exists()):
            continue
        try:
            _write_hooks(path, {} if remove else entries)
            done.append(name)
        except APP_ERRORS:
            step(f"{name}: could not edit {path}; its hooks were left alone")
    return done


def _opencode_dir(home: Path) -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config") / "opencode"


def opencode(home: Path) -> str:
    """OpenCode: any model it runs that can call tools gets the memory. Its config differs from
    the others (`mcp`, a command list, `environment`), and it does not pass a server's
    instructions on to the model, so they go in an instructions file the config lists. A plugin
    records the conversation for the capture service and adds the memory that bears on each
    message before the model answers it (opencode_plugin.js)."""
    folder = _opencode_dir(home)
    if not folder.is_dir() and not shutil.which("opencode"):
        return ""
    folder.mkdir(parents=True, exist_ok=True)
    config = next((folder / n for n in ("opencode.jsonc", "opencode.json")
                   if (folder / n).exists()), folder / "opencode.json")
    try:
        settings = json.loads(config.read_text(encoding="utf-8")) if config.exists() else {}
    except ValueError:                       # comments in the .jsonc: leave the file alone
        step(f"OpenCode: could not edit {config}; add the server there by hand")
        return ""
    from graph_mind.mcp_server import instructions
    guide = folder / "graph-mind.md"
    guide.write_text("# Turnbook memory\n\n" + instructions(True) + "\n", encoding="utf-8")
    from graph_mind.conversation_memory import opencode_root
    plugin = (HERE / "opencode_plugin.js").read_text(encoding="utf-8")
    plugin = plugin.replace("__PYTHON__", json.dumps(sys.executable)).replace(
        "__TRANSCRIPTS__", json.dumps(str(opencode_root())))
    (folder / "plugins").mkdir(exist_ok=True)
    (folder / "plugins" / "graph-mind.js").write_text(plugin, encoding="utf-8")
    settings.setdefault("$schema", "https://opencode.ai/config.json")
    servers = settings.setdefault("mcp", {})
    servers[NAME] = {"type": "local", "command": [sys.executable, *SERVER], "enabled": True,
                     "timeout": 30000,
                     "environment": {**servers.get(NAME, {}).get("environment", {}), **CAPTURED}}
    listed = settings.setdefault("instructions", [])
    if str(guide) not in listed:
        listed.append(str(guide))
    temporary = config.with_suffix(".graph-mind.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, config)
    return "OpenCode"


_KEY_PART = re.compile(r"""\s*(?:([A-Za-z0-9_-]+)|"((?:[^"\\]|\\.)*)"|'([^']*)')\s*(\.|\])""")


def _table_key(line: str):
    """A TOML [table] header's key, as a tuple of its parts; None for anything else. Parts may
    be bare, "basic" (with escapes) or 'literal'; stripping quotes instead read
    [mcp_servers."graph-mind-memory.env"] as our env table and deleted someone else's."""
    text = line.strip()
    if not text.startswith("[") or text.startswith("[["):
        return None
    parts, at = [], 1
    while match := _KEY_PART.match(text, at):
        bare, basic, literal, end = match.groups()
        if basic is not None:
            try:
                basic = json.loads('"' + re.sub(r"\\U([0-9A-Fa-f]{8})",
                                                lambda m: chr(int(m[1], 16)), basic) + '"')
            except ValueError:
                return None
        parts.append(bare if bare is not None else basic if basic is not None else literal)
        at = match.end()
        if end == "]":
            return tuple(parts)
    return None


def _without_codex_block(text: str) -> str:
    """The Codex config without Turnbook's server table (and its env table); the rest kept.
    The table may be written [mcp_servers."graph-mind-memory"] too (TOML allows quoted keys):
    left in place, the table added after it made the file invalid (duplicate table)."""
    ours = (("mcp_servers", NAME), ("mcp_servers", NAME, "env"))
    kept, skipping, dropped = [], False, False
    for line in text.splitlines():
        if line.strip().startswith("["):
            skipping = _table_key(line) in ours
        if skipping:
            dropped = True
        else:
            kept.append(line)
    if not dropped:
        return text
    return "\n".join(kept).rstrip() + "\n"


def _login_item() -> Path:
    """Where the capture service is registered to start at login, on this OS."""
    system = platform.system()
    if system == "Windows":
        return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
                / "Startup" / "Graph-MIND Automatic Capture.lnk")
    if system == "Darwin":
        return Path.home() / "Library" / "LaunchAgents" / "com.graph-mind.capture.plist"
    config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return config / "autostart" / "turnbook-capture.desktop"


def codex(home: Path) -> str:
    config = home / ".codex" / "config.toml"
    if not config.parent.is_dir():
        return ""
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    header = f"[mcp_servers.{NAME}]"
    text = _without_codex_block(text)
    environment = "\n".join(f"{key} = '{value}'" for key, value in CAPTURED.items())
    # not required: a memory server that fails to start must never keep a chat from starting
    text += (f"\n{header}\nstartup_timeout_sec = 60\n"
             f"command = {json.dumps(sys.executable, ensure_ascii=False)}\n"
             f"args = {json.dumps(SERVER)}\n\n"
             f"[mcp_servers.{NAME}.env]\n{environment}\n")
    config.write_text(text, encoding="utf-8")
    return "Codex"


def capture_service(start: bool) -> str:
    from graph_mind.automatic_capture import (
        _atomic_json,
        default_config_path,
        make_config,
    )
    if not default_config_path().exists():
        config = make_config(workspace=str(Path.home()), project_id="project:graph-mind",
                             session_id="service:automatic-capture-v0.2",
                             sessions_root=str(Path.home() / ".codex" / "sessions"))
        # Conversations only: watching a code folder is opt-in.
        config["code_workspaces"], config["workspace_scopes"] = [], {}
        _atomic_json(default_config_path(), config)
    system = platform.system()
    if system == "Windows":
        python = Path(sys.executable).with_name("pythonw.exe")        # no console window
        startup = _login_item()
        startup.parent.mkdir(parents=True, exist_ok=True)
        def ps(value):                   # a PowerShell literal: '' escapes a quote
            return "'" + str(value).replace("'", "''") + "'"
        shortcut = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut({ps(startup)});"
                    f"$s.TargetPath={ps(python)};$s.Arguments={ps(' '.join(CAPTURE))};"
                    f"$s.WorkingDirectory={ps(Path.home())};$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", shortcut], check=True,
                       capture_output=True)
    elif system == "Darwin":
        import plistlib
        python = Path(sys.executable)
        agent = _login_item()
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(plistlib.dumps({
            "Label": "com.graph-mind.capture", "RunAtLoad": True,
            "WorkingDirectory": str(Path.home()), "ProgramArguments": [str(python), *CAPTURE]}))
    else:                                       # Linux desktops: XDG autostart
        python = Path(sys.executable)
        entry = _login_item()
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("[Desktop Entry]\nType=Application\nName=Turnbook capture\n"
                         f"Exec=\"{python}\" {' '.join(CAPTURE)}\nPath={Path.home()}\n"
                         "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    if start:
        from graph_mind.automatic_capture import ensure_service
        ensure_service()
    return "capture service starts at login"


def embedding_model():
    from graph_mind.local_embedder import model_files, model_paths, own_hf_home
    try:
        import onnxruntime  # noqa: F401  (a DLL failure here would leave search on words only)
    except ImportError as error:
        if platform.system() != "Windows":
            raise
        sys.exit(f"\n{error}\n\nThe embedding runtime could not load. Install the Microsoft "
                 "Visual C++ Redistributable (x64), restart, and run this again:\n"
                 "    https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
                 "The apps are already connected; until then recall works by word search only.")
    try:
        model_paths()
    except OSError as error:
        # ~/.cache refused (access denied): move Hugging Face's whole home under ~/.graph-mind,
        # where every Turnbook process looks from now on (local_embedder.use_own_hf_home)
        denied = (isinstance(error, PermissionError) or isinstance(error.__cause__, PermissionError)
                  or "PermissionError" in str(error))
        if not denied:
            raise
        own = own_hf_home()
        own.mkdir(parents=True, exist_ok=True)
        step(f"the usual model folder is not writable; using {own}")
        # huggingface_hub fixed its folders when it was imported: download in a fresh process
        subprocess.run([sys.executable, "-c",
                        "from huggingface_hub import hf_hub_download; "
                        + "; ".join(f"hf_hub_download({model_files()[0]!r}, {name!r})"
                                    for name in model_files()[1])],
                       env={**os.environ, "HF_HOME": str(own),
                            "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}, check=True)
    remove_old_models()


def remove_old_models():
    """The model 0.9.9 and before used (~450 MB): nothing reads it once the apps restart. Through
    Hugging Face's own cache API: its files live in a shared blobs folder, not the repo's."""
    from huggingface_hub import scan_cache_dir
    from huggingface_hub.constants import HF_HUB_CACHE

    from graph_mind.local_embedder import OLD_MODELS, own_hf_home
    for cache in {Path(HF_HUB_CACHE), own_hf_home() / "hub"}:
        try:
            found = scan_cache_dir(cache)
            old = [revision.commit_hash for repo in found.repos if repo.repo_id in OLD_MODELS
                   for revision in repo.revisions]
            if old:
                found.delete_revisions(*old).execute()
        except Exception:            # no cache there, or a file in use: try again next install
            pass


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install Turnbook on this PC.")
    parser.add_argument("--join", metavar="CODE",
                        help="a code to join with: your other PC's (gm1.) or a team invite (gmt1.)")
    parser.add_argument("--name", help="the name teammates see on shared projects (e.g. 고요한)")
    parser.add_argument("--skip-packages", action="store_true")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--no-start", action="store_true", help="register, but start nothing now")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        sys.exit("Turnbook needs Python 3.10 or newer.")
    if args.join:                            # a typo in the code should fail before anything else
        if args.join.strip().startswith("gmt1."):
            from graph_mind.project_brains import _decode_team
            _decode_team(args.join)
        else:
            from graph_mind.sharing import _decode
            _decode(args.join)
    if not args.skip_packages and (CHECKOUT / "pyproject.toml").exists():   # pip: already done
        step("installing packages (the first time takes a few minutes)")
        packages()
    home = Path.home()
    def tried(connect, *args):
        try:
            result = connect(*args)
        except APP_ERRORS as error:
            step(f"{connect.__name__}: skipped ({type(error).__name__}: {error})")
            return []
        return result if isinstance(result, list) else [result]
    hooked = tried(agent_hooks, home)          # first: other_apps marks only hooked apps captured
    apps = [name for name in [*tried(claude_code, home), *tried(claude_desktop, home),
                              *tried(codex, home), *tried(opencode, home),
                              *tried(other_apps, home)] if name]
    step("memory connected to: " + (", ".join(apps) if apps else
                                     "no AI app found yet; install one and run this again"))
    if hooked:
        step("memory added to each message in: " + ", ".join(hooked))
    if "Codex" in hooked:
        step("Codex asks once before it runs a new hook: type /hooks in Codex and trust "
             "Turnbook's")
    if args.name:
        from graph_mind.sharing import _save
        _save({"user_name": " ".join(args.name.split())[:40]})
        step(f"teammates will see you as {args.name}")
    step(capture_service(start=not args.no_start))
    if not args.skip_model:
        step("downloading the local embedding model")
        embedding_model()
    if args.join and args.join.strip().startswith("gmt1."):
        from graph_mind.project_brains import join_team
        joined = join_team(args.join.strip(), args.name)
        projects = ", ".join(joined["projects"]) or "no projects yet"
        step(f"joined the team as {joined['joined_as']}: {projects}")
    elif args.join:
        from graph_mind.sharing import folder, set_folder
        set_folder(args.join)
        where = folder().rsplit("@", 1)[1].split("/")[0]
        step(f"joined the shared brain at {where}")
    print("\nDone. Restart your AI apps so they load the memory.")


def uninstall(argv=None):
    """Undo install: unregister from every AI app, remove the login item, stop the capture
    service. The memory itself stays unless --purge, so reinstalling picks up where it was."""
    parser = argparse.ArgumentParser(description="Remove Turnbook from this PC's AI apps.")
    parser.add_argument("--purge", action="store_true",
                        help="also delete this PC's memory (~/.graph-mind); cannot be undone")
    parser.add_argument("--yes", action="store_true", help="do not ask before --purge")
    args = parser.parse_args(argv)
    home, removed = Path.home(), []
    claude = shutil.which("claude")
    if claude:
        try:
            subprocess.run([claude, "mcp", "remove", "-s", "user", NAME], capture_output=True)
        except OSError:                  # blocked by antivirus or policy: remove the rest anyway
            pass
    targets = ([("Claude Code", home / ".claude.json", "mcpServers")]
               + [("Claude Desktop", r / "claude_desktop_config.json", "mcpServers")
                  for r in _desktop_roots(home)]
               + [(name, config, key) for name, _, config, key, _ in _other_apps(home)]
               + [("OpenCode", _opencode_dir(home) / n, "mcp")
                  for n in ("opencode.jsonc", "opencode.json")])
    for label, config, key in targets:
        if not config.exists():
            continue
        try:
            settings = json.loads(config.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if settings.get(key, {}).pop(NAME, None) is not None:
            guide = str(_opencode_dir(home) / "graph-mind.md")
            if guide in settings.get("instructions", []):
                settings["instructions"].remove(guide)
            temporary = config.with_suffix(".graph-mind.tmp")
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, config)
            removed.append(label)
    # our own file, whatever the config says: a commented opencode.jsonc is skipped above, and
    # the plugin went on recording every OpenCode conversation after uninstall
    plugin = _opencode_dir(home) / "plugins" / "graph-mind.js"
    if plugin.exists():
        plugin.unlink()
        removed.append("OpenCode plugin")
    codex_config = home / ".codex" / "config.toml"
    if codex_config.exists():
        text = codex_config.read_text(encoding="utf-8")
        if text != _without_codex_block(text):
            codex_config.write_text(_without_codex_block(text), encoding="utf-8")
            removed.append("Codex")
    removed += agent_hooks(home, remove=True)
    step("memory removed from: " + (", ".join(dict.fromkeys(removed)) or "no app had it"))
    item = _login_item()
    if item.exists():
        if platform.system() == "Darwin":
            subprocess.run(["launchctl", "unload", str(item)], capture_output=True)
        item.unlink()
        step("capture service no longer starts at login")
    from graph_mind.automatic_capture import default_service_state_dir
    try:
        import psutil
        service = psutil.Process(int((default_service_state_dir() / "service.pid")
                                     .read_text(encoding="ascii")))
        if "capture_cli" in " ".join(service.cmdline()):
            service.terminate()
            step("capture service stopped")
    except Exception:                            # no pid file, not running, or already gone
        pass
    from graph_mind.paths import graph_mind_home
    data = graph_mind_home()
    if args.purge and data.exists():
        if not args.yes and input(f"Delete {data} and everything this PC remembers? "
                                  "This cannot be undone. [y/N] ").lower() != "y":
            print("Memory kept.")
        else:
            if (data / "postgres" / "postmaster.pid").exists():     # this PC hosts the hub
                try:
                    from graph_mind import sharing
                    sharing.stop_postgres(data / "postgres")
                except Exception:
                    pass
            shutil.rmtree(data, ignore_errors=True)
            step(f"deleted {data}")
    elif data.exists():
        step(f"your memory is kept in {data} (turnbook-uninstall --purge deletes it)")
    print("\nDone. Restart your AI apps. To remove the program too: "
          "pip uninstall turnbook")


if __name__ == "__main__":
    main()
