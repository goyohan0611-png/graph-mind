"""One line on a new PC: install Graph-MIND and plug it into every AI app found there.

    python install.py                      # this PC keeps its own brain
    python install.py --join gm1.XXXX      # ...and joins the brain another PC shared

It installs the Python packages, registers the memory server with Claude Code, Claude Desktop and
Codex (whichever are installed), starts the capture service at login, and downloads the local
embedding model so the first question is not the one that waits for it. Running it again is
safe, and after moving this folder it re-points everything at the new place.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import json
import os
import platform
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SERVER = HERE / "graph_mind_mcp_server.py"
NAME = "graph-mind-memory"
ENV = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def step(message):
    print("-", message, flush=True)


def packages():
    done = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                           "-q", "-r", str(HERE / "requirements.txt")])
    if done.returncode:
        # PyTorch ships files nested deep enough to pass Windows' 260-character path limit when
        # Python lives under a long folder: pip then fails halfway with "No such file".
        sys.exit("\nPackage install failed. On Windows the usual cause is the 260-character path "
                 "limit:\n  enable long paths (run as administrator, then restart):\n"
                 "    reg add HKLM\\SYSTEM\\CurrentControlSet\\Control\\FileSystem "
                 "/v LongPathsEnabled /t REG_DWORD /d 1 /f\n"
                 "  or install Python or this folder under a shorter path, e.g. C:\\graph-mind")


def _json_entry(path: Path, **extra) -> None:
    settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    servers = settings.setdefault("mcpServers", {})
    environment = {**servers.get(NAME, {}).get("env", {}), **ENV}   # keep the user's own settings
    servers[NAME] = {**extra, "command": sys.executable, "args": [str(SERVER)], "env": environment}
    temporary = path.with_suffix(".graph-mind.tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def claude_code(home: Path) -> str:
    claude = shutil.which("claude")      # full path: an npm install is claude.CMD, which
    if claude:                           # Windows will not launch by its bare name
        subprocess.run([claude, "mcp", "remove", "-s", "user", NAME], capture_output=True)
        environment = [flag for key, value in ENV.items() for flag in ("-e", f"{key}={value}")]
        # the name before -e: the CLI's -e takes every value that follows, the name included
        subprocess.run([claude, "mcp", "add", "-s", "user", NAME, *environment, "--",
                        sys.executable, str(SERVER)], check=True, capture_output=True)
        return "Claude Code"
    if (home / ".claude.json").exists():
        _json_entry(home / ".claude.json", type="stdio")
        return "Claude Code"
    return ""


def claude_desktop(home: Path) -> list[str]:
    roots = [Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "Claude",
             home / "Library" / "Application Support" / "Claude"]
    packages_dir = home / "AppData" / "Local" / "Packages"
    if packages_dir.is_dir():               # the Microsoft Store build keeps its own copy
        roots += [p / "LocalCache" / "Roaming" / "Claude" for p in packages_dir.glob("Claude_*")]
    done = []
    for root in roots:
        if root.is_dir():
            _json_entry(root / "claude_desktop_config.json")
            done.append("Claude Desktop")
    return done[:1]


def codex(home: Path) -> str:
    config = home / ".codex" / "config.toml"
    if not config.parent.is_dir():
        return ""
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    header = f"[mcp_servers.{NAME}]"
    if header in text:                       # drop the old block (and its env table), keep the rest
        kept, skipping = [], False
        for line in text.splitlines():
            if line.strip().startswith("["):
                skipping = line.strip() in (header, f"[mcp_servers.{NAME}.env]")
            if not skipping:
                kept.append(line)
        text = "\n".join(kept).rstrip() + "\n"
    environment = "\n".join(f"{key} = '{value}'" for key, value in ENV.items())
    # required: otherwise Codex leaves out a server still starting when the first question comes
    text += (f"\n{header}\nrequired = true\nstartup_timeout_sec = 60\n"
             f"command = '{sys.executable}'\nargs = ['{SERVER}']\n\n"
             f"[mcp_servers.{NAME}.env]\n{environment}\n")
    config.write_text(text, encoding="utf-8")
    return "Codex"


def capture_service(start: bool) -> str:
    sys.path.insert(0, str(HERE))
    from automatic_capture import _atomic_json, default_config_path, default_service_state_dir, make_config
    if not default_config_path().exists():
        config = make_config(workspace=str(HERE), project_id="project:graph-mind",
                             session_id="service:automatic-capture-v0.2",
                             sessions_root=str(Path.home() / ".codex" / "sessions"))
        # Conversations only. Watching a code folder is opt-in: this folder is Graph-MIND itself,
        # or site-packages after a pip install, and the service would rescan it every 2 minutes.
        config["code_workspaces"], config["workspace_scopes"] = [], {}
        _atomic_json(default_config_path(), config)
    script = HERE / "automatic_capture_cli.py"
    system = platform.system()
    if system == "Windows":
        python = Path(sys.executable).with_name("pythonw.exe")        # no console window
        startup = (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu"
                   / "Programs" / "Startup" / "Graph-MIND Automatic Capture.lnk")
        startup.parent.mkdir(parents=True, exist_ok=True)
        shortcut = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{startup}');"
                    f"$s.TargetPath='{python}';$s.Arguments='\"{script}\" run';"
                    f"$s.WorkingDirectory='{HERE}';$s.Save()")
        subprocess.run(["powershell", "-NoProfile", "-Command", shortcut], check=True,
                       capture_output=True)
    elif system == "Darwin":
        import plistlib
        python = Path(sys.executable)
        agent = Path.home() / "Library" / "LaunchAgents" / "com.graph-mind.capture.plist"
        agent.parent.mkdir(parents=True, exist_ok=True)
        agent.write_bytes(plistlib.dumps({
            "Label": "com.graph-mind.capture", "RunAtLoad": True, "WorkingDirectory": str(HERE),
            "ProgramArguments": [str(python), str(script), "run"]}))
    else:                                       # Linux desktops: XDG autostart
        python = Path(sys.executable)
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
        entry = config / "autostart" / "graph-mind-capture.desktop"
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("[Desktop Entry]\nType=Application\nName=Graph-MIND capture\n"
                         f"Exec=\"{python}\" \"{script}\" run\nPath={HERE}\n"
                         "X-GNOME-Autostart-enabled=true\n", encoding="utf-8")
    if start:
        import psutil
        pid_file = default_service_state_dir() / "service.pid"
        try:
            running = psutil.pid_exists(int(pid_file.read_text(encoding="ascii")))
        except (OSError, ValueError):
            running = False
        if not running:
            detach = ({"creationflags": subprocess.DETACHED_PROCESS} if system == "Windows"
                      else {"start_new_session": True})
            subprocess.Popen([str(python), str(script), "run"], cwd=HERE, **detach)
    return "capture service starts at login"


def embedding_model():
    sys.path.insert(0, str(HERE))
    from local_embedder import LOCAL_MODEL, own_hf_home
    try:
        from transformers import AutoModel, AutoTokenizer
    except OSError as error:
        if "1114" not in str(error):
            raise
        # WinError 1114 on c10.dll: PyTorch needs the Microsoft Visual C++ runtime, which a fresh
        # Windows often lacks. Everything else is installed; recall uses word search until then.
        sys.exit(f"\n{error}\n\nPyTorch could not load. Install the Microsoft Visual C++ "
                 "Redistributable (x64), restart, and run this again:\n"
                 "    https://aka.ms/vs/17/release/vc_redist.x64.exe\n"
                 "The apps are already connected; until then recall works by word search only.")

    try:
        AutoTokenizer.from_pretrained(LOCAL_MODEL)
        AutoModel.from_pretrained(LOCAL_MODEL)
    except OSError as error:
        # ~/.cache refused (access denied): move Hugging Face's whole home under ~/.graph-mind,
        # where every Graph-MIND process looks from now on (local_embedder.use_own_hf_home)
        if "PermissionError" not in str(error) and not isinstance(error.__cause__, PermissionError):
            raise
        own = own_hf_home()
        own.mkdir(parents=True, exist_ok=True)
        step(f"the usual model folder is not writable; using {own}")
        # huggingface_hub fixed its folders when it was imported: download in a fresh process
        subprocess.run([sys.executable, "-c",
                        "from transformers import AutoModel, AutoTokenizer; "
                        f"AutoTokenizer.from_pretrained({LOCAL_MODEL!r}); "
                        f"AutoModel.from_pretrained({LOCAL_MODEL!r})"],
                       env={**os.environ, "HF_HOME": str(own),
                            "HF_HUB_DISABLE_SYMLINKS_WARNING": "1"}, check=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install Graph-MIND on this PC.")
    parser.add_argument("--join", metavar="CODE", help="a gm1. code from `share` on another PC")
    parser.add_argument("--skip-packages", action="store_true")
    parser.add_argument("--skip-model", action="store_true")
    parser.add_argument("--no-start", action="store_true", help="register, but start nothing now")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        sys.exit("Graph-MIND needs Python 3.10 or newer.")
    if args.join:                            # a typo in the code should fail before anything else
        sys.path.insert(0, str(HERE))
        from brain_log import _decode
        _decode(args.join)
    if not args.skip_packages and (HERE / "requirements.txt").exists():   # pip: already done
        step("installing packages (the first time takes a few minutes)")
        packages()
    home = Path.home()
    apps = [name for name in [claude_code(home), *claude_desktop(home), codex(home)] if name]
    step("memory connected to: " + (", ".join(apps) if apps else
                                     "no AI app found yet; install one and run this again"))
    step(capture_service(start=not args.no_start))
    if not args.skip_model:
        step("downloading the local embedding model")
        embedding_model()
    if args.join:
        from brain_log import folder, set_folder
        set_folder(args.join)
        where = folder().rsplit("@", 1)[1].split("/")[0]
        step(f"joined the shared brain at {where}")
    print("\nDone. Restart your AI apps so they load the memory.")


if __name__ == "__main__":
    main()
