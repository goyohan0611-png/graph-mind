# Graph-MIND

**Your AI memory, on your machine. Switch models; keep the memory.**

Graph-MIND records your conversations with Claude Code, Codex and Claude Desktop word for word, in a
store on your own PC. When you ask something that depends on the past, it gives the model you are
using a few thousand tokens of the conversations that answer it. Saving needs no model call and costs
no tokens.

> Status: **alpha (v0.1)**. Tested on Windows 11 with one user's daily use. macOS and Linux have not
> been tried yet.

## How well it recalls

LongMemEval_S, the standard long-term chat memory benchmark. The answer model is gpt-5-mini and the
official judge is gpt-4o-2024-08-06. Every decision was fixed before the run. Full method, negative
results and corrections are in [REPORT.md](REPORT.md).

| | accuracy | model tokens spent saving |
|---|---|---|
| **Graph-MIND, all 500 questions** | **88.8%** (444/500) | **0** |
| Graph-MIND, the 380 never used for tuning | 86.8% | 0 |

Head to head on 40 pre-registered questions nobody had tuned on, same answer model, same prompt, same
judge:

| | accuracy | model tokens spent saving, per question |
|---|---|---|
| **Graph-MIND** (shipped path, earlier 20-item packet) | **85.0%** | **0** |
| MemPalace 3.10.0 | 57.5% | 0 |
| Mem0 2.2.1 | 52.5% | ~640k |

Both gaps are significant (exact McNemar p = 0.002 and 0.003). Vendors' own LongMemEval reports use
other answer models and setups and were not reproduced here. Supermemory reports 85.2%, Zep 71.2%.

## Install

Python 3.10 or newer, then from this folder:

```
python install.py
```

The installer does five things:

- installs the packages (PyTorch is the large one, about 2 GB);
- registers the memory server with every app it finds: Claude Code, Claude Desktop (including the
  Microsoft Store build) and Codex;
- sets the capture service to start at login;
- downloads the local embedding model;
- tells you to restart your AI apps.

Running it again is safe. Run it again after moving this folder.

## What it captures

| app | captured |
|---|---|
| Codex: terminal, VS Code, ChatGPT desktop's work mode | every turn |
| Claude Code: terminal, VS Code, Claude desktop's Code tab | every turn |
| Claude desktop Cowork | every turn |
| Claude desktop chat | what the model saves with `brain_remember` |

The capture service reads each app's local transcript files every few seconds. It masks secrets
before storing anything: API keys, GitHub, AWS, Google and Slack tokens, private keys, and passwords
inside URLs. It embeds new turns on the PC itself, so they are searchable by meaning before anyone
asks.

## One memory across PCs

On the PC that holds the memory, tell its AI *"let my other PCs use this memory"*. It answers with a
connection code (`gm1.…`). On the other PC:

```
python install.py --join gm1.…
```

That PC now reads and writes the same memory: over the local network in the office, over
[Tailscale](https://tailscale.com) from anywhere else. Under the hood:

- the memory lives in a Postgres server on the first PC, which the installer sets up;
- other PCs log in with a generated password, as a role that can only reach the memory database;
- the Windows firewall rule admits only the local network and Tailscale.

The code contains that password, so do not post it anywhere public.

A synced folder (OneDrive, Google Drive, Dropbox) works too: tell the AI to use that folder as its
memory.

## Privacy

Everything stays on your machines. Nothing is sent anywhere except the requests your own model
client already makes. There is no cloud service, and no account.

## Reproducing the numbers

1. Download the LongMemEval_S data (`longmemeval_s_cleaned.json` from the
   [LongMemEval](https://github.com/xiaowu0162/LongMemEval) release) into `external/longmemeval/`.
2. Set `OPENAI_API_KEY` for the answer model and the judge.
3. Run the commands in REPORT.md §10.

`runs/` holds the question lists, answers, verdicts and pre-registrations behind every number
above.

## Tests

```
python -m unittest discover -p "test_*.py"
```

## Known limits

- Windows only so far.
- Capture depends on each app's transcript format, which is not a public interface. An app update
  can stop capture until Graph-MIND is updated.
- Multi-session questions (counting or combining across many conversations) are the weakest type,
  at 80%.
- The code base still carries modules from the research phase. It will be slimmed down.

See REPORT.md §9 for the rest.

## License

[AGPL-3.0](LICENSE). Building a service on Graph-MIND without publishing your changes needs a
commercial license: see [COMMERCIAL.md](COMMERCIAL.md). Contributions are accepted under the
[CLA](CLA.md).
