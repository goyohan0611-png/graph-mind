<div align="center">

# Graph-MIND

Local-first memory for AI coding assistants. Verbatim storage, on your own PC:
**88.8% on LongMemEval with zero model calls at write time.**

[![][license-shield]][license-link]
[![][python-shield]][python-link]
[![][platform-shield]][platform-link]
[![][longmemeval-shield]][benchmarks-link]

*Switch models; keep the memory.*

</div>

> [!NOTE]
> **Alpha (v0.1).** Tested on Windows 11 under one user's daily use, and in a fresh-install, two-PC
> end-to-end run. macOS and Linux have not been tried yet.

---

## What it is

Graph-MIND records every conversation you have with **Claude Code, Codex and Claude Desktop**, word
for word, in a store on your own PC. When a question depends on the past, the model you are using
calls Graph-MIND over MCP and gets back a few thousand tokens of the conversations that answer it.

- **Nothing is summarised or rewritten.** Saving is a database write. No model call, no tokens.
- **Search is hybrid.** Your words are embedded on your PC (multilingual MiniLM, no API) and fused
  with keyword search. Korean and English both work.
- **Capture is automatic.** A background service reads each app's local transcripts, masks secrets,
  stores the turns and embeds them as they arrive.
- **One memory across your PCs.** One sentence to the AI on the first PC, one command on the
  others (see [below](#one-memory-across-pcs)).
- **Nothing leaves your machines.** There is no cloud, no account and no telemetry.

---

## Benchmarks

All numbers below come from files in this repository: the pre-registrations, each question's
answer, and the judge's verdict on it, under [`runs/`](runs). The method is in
[REPORT.md](REPORT.md), including the failures and the corrections.

**LongMemEval_S: answer accuracy, 500 questions.** The answer model is gpt-5-mini; the judge is the
official gpt-4o-2024-08-06.

| | accuracy | model tokens at write time | packet read per question |
|---|---|---|---|
| **All 500 questions** | **88.8%** (444/500) | **0** | 3.4k tokens |
| The 380 never used for tuning | **86.8%** (330/380) | 0 | 3.4k tokens |

**Head to head: same 40 questions, same answer model, prompt and judge.** The run was
pre-registered with code hashes; nobody had tuned on these questions.

| system | accuracy | model tokens at write time, per question |
|---|---|---|
| **Graph-MIND** (shipped path, earlier 20-item packet) | **85.0%** | **0** |
| MemPalace 3.10.0 | 57.5% | 0 |
| Mem0 2.2.1 (open source, latest on PyPI) | 52.5% | ~640k |

Graph-MIND's lead over both is significant (exact McNemar p = 0.002 and 0.003).

**Reading other published numbers.** They measure different things, so they do not compare
directly with the tables above:

- **MemPalace's 96.6%** is retrieval recall (R@5): is the right session among the five returned?
  This table measures whether the final answer is correct.
- **Mem0's 94.4%** is its managed cloud platform, which includes proprietary components. The open
  source package tested here is a different system.
- **Mastra (94.87%), Emergence (86%), Supermemory (85.2%) and Zep (71.2%)** report their own setups
  and answer models. They were not reproduced here.

As far as we know, everything above 85% on that list runs a model over your conversations when it
saves them. Graph-MIND does not.

---

## Install

Requires Python 3.10+ (64-bit) on Windows.

```bash
git clone https://github.com/goyohan0611-png/graph-mind.git
cd graph-mind
python install.py
```

The installer:

- installs the packages (PyTorch CPU is the large one, about 2 GB);
- registers the MCP server with every app it finds: Claude Code, Claude Desktop (including the
  Microsoft Store build) and Codex;
- starts the capture service at login;
- downloads the embedding model.

Then restart your AI apps. Running the installer again is safe. Run it again if you move the folder.

> [!TIP]
> If `pip` fails with "No such file or directory", Windows' 260-character path limit is the usual
> cause. Clone to a short path such as `C:\graph-mind`, or enable long paths. The installer prints
> the command for that.

---

## What it captures

| app | captured |
|---|---|
| Codex: terminal, VS Code, ChatGPT desktop's work mode | every turn |
| Claude Code: terminal, VS Code, Claude desktop's Code tab | every turn |
| Claude desktop: Cowork | every turn |
| Claude desktop: chat | what the model saves with `brain_remember` |

Before anything is stored, these are masked: API keys, tokens from GitHub, AWS, Google and Slack,
private keys, and passwords inside URLs.

---

## MCP tools

| tool | what it does |
|---|---|
| `brain_context` | a bounded packet of the memories and conversation turns that answer a request |
| `brain_recall` | inspect memories and captured turns directly |
| `brain_remember` | save a sourced memory (secrets masked) |
| `brain_associate` | recall from a vague cue |
| `brain_timeline` | everything about one thing, oldest first, with replaced entries marked |
| `brain_folder` | show or choose where the memory lives; share it with other PCs |
| `brain_index` | bring an imported backlog up to date |
| `conversation_recall` | search the captured turns themselves |
| `code_activity` | what changed in a project, file or symbol, and when |
| `memory_record` | record a development event or decision |
| `project_status` | current state of a project |
| `resume_project` | everything needed to pick a project back up |
| `explain_decision` | a decision, its reason and its history |

---

## One memory across PCs

On the PC that holds the memory, tell its AI:

> *"Let my other PCs use this memory."*

It replies with a connection code (`gm1.…`). On each other PC:

```bash
python install.py --join gm1.…
```

The memory then lives in a Postgres server on the first PC, which Graph-MIND sets up itself. Other
PCs reach it over the local network in the office, or over [Tailscale](https://tailscale.com) from
anywhere. Each connection tries the addresses in turn and uses the first that answers.

Other PCs log in with a generated password, as a role that can reach only the memory database. The
Windows firewall rule admits only the local network and Tailscale. The code contains the password:
do not post it publicly.

A synced folder also works. Tell the AI *"use my Google Drive's Graph-MIND folder as my memory"* on
each PC.

---

## Reproducing the benchmarks

1. Download `longmemeval_s_cleaned.json` from [LongMemEval](https://github.com/xiaowu0162/LongMemEval)
   into `external/longmemeval/`.
2. Set `OPENAI_API_KEY` for the answer model and the judge.
3. Run the commands in [REPORT.md §10](REPORT.md#10-reproducing).

A full 500-question run costs about US$3.

## Tests

```bash
python -m unittest discover -p "test_*.py"
```

---

## Known limits

- **Windows only so far.**
- **Capture follows each app's transcript format,** which is not a public interface. An app update
  can stop capture until Graph-MIND is updated.
- **Multi-session questions are the weakest type at 80%.** These are questions that count or
  combine facts across many conversations.
- **The code base still carries modules from the research phase.** It will be slimmed down.

The full list is in [REPORT.md §9](REPORT.md#9-known-limits).

## Contributing

Issues and pull requests are welcome. Contributions are accepted under the [CLA](CLA.md), which
keeps the dual license possible.

## License

[AGPL-3.0](LICENSE). Anyone running a modified Graph-MIND as a network service, such as the memory
behind a support chatbot, must publish that source. A [commercial license](COMMERCIAL.md) is
available for products that cannot.

<!-- Links -->
[license-shield]: https://img.shields.io/badge/license-AGPL--3.0-4dc9f6?style=flat-square
[license-link]: LICENSE
[python-shield]: https://img.shields.io/badge/python-3.10+-7dd8f8?style=flat-square&logo=python&logoColor=white
[python-link]: https://www.python.org/
[platform-shield]: https://img.shields.io/badge/platform-Windows-b0e8ff?style=flat-square
[platform-link]: #known-limits
[longmemeval-shield]: https://img.shields.io/badge/LongMemEval-88.8%25-2ea44f?style=flat-square
[benchmarks-link]: #benchmarks
