<!-- mcp-name: io.github.goyohan0611-png/graph-mind -->
<div align="center">

# Graph-MIND

### Your AI forgets everything when the session ends. This one remembers, and it never calls an LLM to do it.

**88.8% on LongMemEval. Zero model tokens spent saving memory. Runs on your own PC.**

[![][license-shield]][license-link]
[![][python-shield]][python-link]
[![][platform-shield]][platform-link]
[![][longmemeval-shield]][benchmarks-link]
[![][ci-shield]][ci-link]
[![][pypi-shield]][pypi-link]
[![goyohan0611-png/graph-mind MCP server](https://glama.ai/mcp/servers/goyohan0611-png/graph-mind/badges/score.svg)](https://glama.ai/mcp/servers/goyohan0611-png/graph-mind)

*Decide something in Claude Code on Monday. Ask Codex on Wednesday. It knows.*

<img src="demo/demo.gif" alt="A decision made in Claude Code on Monday, recalled from Codex on Wednesday" width="760">

</div>

## Install

Windows: download [Graph-MIND-Setup.cmd](https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/Graph-MIND-Setup.cmd)
and double-click it, or in PowerShell:

```powershell
irm https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.ps1 | iex
```

macOS / Linux:

```bash
curl -LsSf https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.sh | sh
```

No Python needed; nothing else to set up. Restart your AI apps afterwards. Already have Python?
`pip install graph-mind-memory && graph-mind-install`. More: [docs/install.md](docs/install.md).

## What it does

- **Remembers every conversation** with Claude Code, Codex and Claude Desktop, word for word, on
  your own PC. Saving is a database write: no model call, no tokens.
- **Any AI picks up where another left off.** When a question depends on the past, the model calls
  Graph-MIND over MCP and gets back the few thousand tokens that answer it. Korean and English.
- **One memory across your PCs.** Tell the AI *"let my other PCs use this memory"*, paste the code it gives you
  to the AI on the other PC. Office network or [Tailscale](https://tailscale.com), over TLS,
  every line encrypted before it leaves a PC.
- **Yours.** No account, no cloud of its own, no telemetry. Secrets are masked before anything is
  stored, and *"delete the password I typed earlier"* deletes it everywhere. [docs/privacy.md](docs/privacy.md)
- **Teams, if you want them.** Share a project's decisions and progress with teammates' AIs, never
  your personal memory; files stay in Slack and GitHub. [docs/teams.md](docs/teams.md)

## Does it work?

Same 40 questions, same answer model, same official judge, pre-registered before anyone looked:

| | accuracy | model tokens spent **saving** memory |
|---|---|---|
| **Graph-MIND** | **85.0%** | **0** |
| MemPalace 3.10.0 | 57.5% | 0 |
| Mem0 2.2.1 (open source) | 52.5% | ~640,000 per question |

On all 500 LongMemEval questions: **88.8%**. Every question, answer and verdict is in
[`research/runs/`](research/runs); method, failures and how to rerun it:
[docs/benchmarks.md](docs/benchmarks.md).

> [!NOTE]
> **Alpha.** Used daily on Windows. macOS and Linux pass every test in CI but have not seen daily
> use yet.

## More

[MCP tools](docs/install.md#mcp-tools) · [See your memory in 3D](docs/privacy.md#seeing-your-memory)
(`graph-mind-view`) · [Changelog](CHANGELOG.md) · [Security](SECURITY.md) ·
Development: `pip install -r requirements.txt`, then `python -m unittest discover -s tests`

Contributions are welcome under the [CLA](CLA.md). License: [AGPL-3.0](LICENSE); a
[commercial license](COMMERCIAL.md) is available for products that cannot publish their changes.

<!-- Links -->
[license-shield]: https://img.shields.io/badge/license-AGPL--3.0-4dc9f6?style=flat-square
[license-link]: LICENSE
[python-shield]: https://img.shields.io/badge/python-3.10+-7dd8f8?style=flat-square&logo=python&logoColor=white
[python-link]: https://www.python.org/
[platform-shield]: https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-b0e8ff?style=flat-square
[platform-link]: docs/benchmarks.md#known-limits
[longmemeval-shield]: https://img.shields.io/badge/LongMemEval-88.8%25-2ea44f?style=flat-square
[benchmarks-link]: docs/benchmarks.md
[ci-shield]: https://img.shields.io/github/actions/workflow/status/goyohan0611-png/graph-mind/tests.yml?style=flat-square&label=tests
[ci-link]: https://github.com/goyohan0611-png/graph-mind/actions/workflows/tests.yml
[pypi-shield]: https://img.shields.io/pypi/v/graph-mind-memory?style=flat-square&label=pypi
[pypi-link]: https://pypi.org/project/graph-mind-memory/
