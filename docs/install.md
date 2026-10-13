# Installing Graph-MIND

## One step, no Python needed

Windows: download [Graph-MIND-Setup.cmd](https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/Graph-MIND-Setup.cmd)
and double-click it (Windows may ask once whether to run a downloaded file), or in PowerShell:

```powershell
irm https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.ps1 | iex
```

macOS or Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.sh | sh
```

It installs [uv](https://docs.astral.sh/uv/) for your user (no administrator rights), which brings
its own Python; installs Graph-MIND with it; and connects every AI app it finds. Restart your AI
apps afterwards.

## With Python already set up

Python 3.10+ (64-bit); Windows, macOS or Linux, on x86-64 or 64-bit ARM:

```bash
pip install graph-mind-memory
graph-mind-install
```

Or from source:

```bash
git clone https://github.com/goyohan0611-png/graph-mind.git
cd graph-mind
python src/install.py
```

## What the installer does

- installs the packages (no PyTorch: the embedding model runs on ONNX Runtime);
- registers the MCP server with every app it finds: Claude Code, Claude Desktop (including the
  Microsoft Store build), Codex, Cursor, Windsurf, Gemini CLI, VS Code (Copilot), Cline and
  LM Studio;
- starts the capture service at login (Windows Startup folder, a macOS LaunchAgent, or an XDG
  autostart entry on Linux);
- downloads the embedding model (about 470 MB, once).

Running it again is safe; run it again if you move the folder. Options: `--name "Your Name"` (the name
teammates see), `--join <code>` (another PC's memory or a team invite). The server is also listed
in the [MCP Registry](https://registry.modelcontextprotocol.io) as
`io.github.goyohan0611-png/graph-mind`.

The first time a machine hosts a memory or a hub, Graph-MIND downloads Postgres 16 for it once (12
MB, 46 MB on Windows; checked against a pinned hash) and keeps it in the user's cache folder.

## MCP tools

| tool | what it does |
|---|---|
| `brain_context` | a bounded packet of the past turns and memories that answer a request; `recent=true` for "where did we leave off?" |
| `brain_recall` | look memories and captured turns up directly; `entity=` for everything about one thing, in order |
| `brain_remember` | save a sourced memory or decision (secrets masked) |
| `brain_folder` | where the memory lives; share it with your other PCs; join with a code |
| `brain_forget` | "delete the password I typed": lists masked candidates, deletes only what you pick, everywhere |
| `code_activity` | what changed in a project, file or symbol, and when (when a code folder is watched) |
| `brain_checkpoint` | [project brains](teams.md), for teams that use them |

Few tools on purpose: every tool's description is read by the model on every turn, and similar
tools get confused with each other.

## Trouble

- **`graph-mind-install` is "not recognized"**: pip put it in a Scripts folder that is not on your
  PATH (common with the Windows Python install manager). `python -m install` runs the same thing.
- **A DLL load error**: Windows is missing the Microsoft Visual C++ runtime the embedding runtime
  needs. Install [vc_redist.x64.exe](https://aka.ms/vs/17/release/vc_redist.x64.exe), restart, and
  run the installer again.
- **pip fails with "No such file or directory"**: Windows' 260-character path limit. Clone to a
  short path such as `C:\graph-mind`, or enable long paths (the installer prints the command).

## Removing it

`graph-mind-uninstall` unregisters it from every app, removes the login item and stops the capture
service. Your memory stays in `~/.graph-mind` unless you add `--purge`. Then
`pip uninstall graph-mind-memory` (or `uv tool uninstall graph-mind-memory`) removes the program.
