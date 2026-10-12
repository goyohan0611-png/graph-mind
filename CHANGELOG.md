# Changelog

Upgrade: `python -m pip install --upgrade graph-mind-memory`, then `graph-mind-install`.
Memory is kept across upgrades (checked in CI on Windows, Linux and macOS).

## 0.6.4 — 2026-10-12
- Mitigates intermittent Postgres initialization and start failures on Windows with a bounded
  retry (up to ten tries each). On the PC where it was seen, initdb succeeded in 3-6 of 10
  identical runs; with the retry, three fresh hubs out of three came up. Interference from
  endpoint security software is suspected but the cause is not confirmed, and inside the test
  harness all ten tries can still fail. To be closed before a multi-user team hub ships.

## 0.6.3 — 2026-10-12
- `graph-mind-view`: regions show from the first frame (nodes start near their region), instead of
  depending on how long the browser runs the layout.

## 0.6.2 — 2026-10-12
- `graph-mind-view`: fixed a black page when opened from disk ("reading 'tick'" on every frame:
  the layout was restarted before it existed). Forces are set first, the data last.

## 0.6.1 — 2026-10-12
- `graph-mind-view` keeps the 3D library inside the page (fetched once by Python), so the page
  opens from disk even where the browser would not load it from the CDN; it stayed black there.
- Region names leave out ids and hashes (words with digits).

## 0.6.0 — 2026-10-12
- `graph-mind-view`: your memory as an interactive 3D graph in the browser. Turns and memories
  are points, joined by the associations recall already keeps; related ones gather into coloured
  regions named by their topic words. `--no-text` writes a copy with no conversation text, safe
  to screenshot or share.

## 0.5.5 — 2026-10-12
- Recall stays fast as memory grows. Meaning search keeps its pieces and vectors in memory and
  adds only the turns captured since the previous question, instead of rebuilding everything per
  question. Measured on synthetic stores: 100,000 turns 6.3 s → 1.3 s per question, 20,000 turns
  1.5 s → 0.3 s; on this PC's real memory 165 ms → 37 ms. Same rankings (44 real queries; only
  the order of identical messages with identical scores can differ).

## 0.5.4 — 2026-10-12
- Faster recall: `brain_context` no longer repeats every item next to the context text it
  already contains, so the model reads about 60% less (43K → 16K characters on a real memory).
  The result lists the item `ids` instead.
- Recall itself takes about half the time (0.9 s → 0.5 s on a real memory): one open connection
  to the shared hub instead of a new one per question, and memory titles are normalized once.

## 0.5.3 — 2026-10-12
Run `graph-mind-install` after upgrading: it rewrites the app registrations.
- A memory server that fails to start no longer keeps Codex from starting a chat (the Codex entry
  is no longer `required`).
- Faster replies in Claude Code and Codex: their conversations are already saved by the capture
  service, so the model is told not to call `brain_remember` for ordinary chat (only when asked
  to remember something). Apps without capture keep saving proactively.
- The installer no longer stops when Windows refuses to launch the `claude` CLI; it writes
  Claude Code's settings file instead.
- Repository layout: the product is in `src/`, tests in `tests/`, benchmarks and REPORT.md in
  `research/`. The installed package is unchanged.

## 0.5.2 — 2026-10-12
- Secret masking can be turned off with `"mask_secrets": false` in `~/.graph-mind/config.json`,
  for people running their own models who want keys remembered verbatim. On by default.

## 0.5.1 — 2026-10-12
- If an app keeps writing transcripts but no conversation comes out of them (its format
  changed), the capture service warns instead of silently saving nothing. The assistant sees the
  warning in `brain_context` / `brain_folder`; `graph-mind-capture status` shows it too.
- CI: upgrading from the previous release keeps the saved memory.

## 0.5.0 — 2026-10-12
- Shared memory is encrypted at rest: each line on the hub is sealed (Fernet, key derived from
  the share password). Existing lines are sealed when sharing starts. **PCs on 0.4 or older
  cannot read sealed lines — upgrade every PC.**
- The installer connects every MCP client it finds: Claude Code, Claude Desktop, Codex, Cursor,
  Windsurf, Gemini CLI, LM Studio, VS Code (Copilot) and Cline.

## 0.4.0 — 2026-10-12
- Embeddings run on ONNX Runtime instead of PyTorch: identical vectors, about 8x faster model
  load, no 2 GB torch install.

## 0.3.4 — 2026-10-12
- An app's first question no longer waits for the embedding model to load (38.8 s → about 2.5 s).

## 0.3.3 — 2026-10-12
- Re-sharing sends only what the shared memory does not have yet.

## 0.3.2 — 2026-10-12
- The shared hub keeps running after the app that started it closes; it restarts when found down,
  and lines that cannot be sent wait in a local outbox.
- `graph-mind-uninstall` (`--purge` also deletes the memory). SECURITY.md.
- Dockerfile for MCP directories, checked in CI.

## 0.3.1 — 2026-10-08
- Masks passwords written after their name (`password:`, `비밀번호:` …) and unknown
  machine-random tokens.

## 0.3.0 — 2026-10-08
- `brain_forget`: ask the assistant to delete something; it lists matches, deletes what you pick,
  and the deletion exchange itself is not remembered.
- Scheduled checks: fresh PyPI install daily, test suite weekly.

## 0.2.1 — 2026-10-08
- `graph-mind-forget`: remove a phrase from every PC's memory.

## 0.2.0 — 2026-10-08
- Five MCP tools instead of thirteen. Tests never touch the developer's own memory.

## 0.1.1 – 0.1.5 — 2026-10-08
- Installs on Python 3.13/3.14; pip installs capture conversations only.
- Clear message for a missing Visual C++ runtime; model cache falls back to `~/.graph-mind`
  when `~/.cache` is not writable.

## 0.1.0 — 2026-10-08
- First release: local memory engine for AI coding assistants, on PyPI and the MCP Registry.
  Automatic conversation capture, context packets, sharing between PCs, login item on
  Windows, macOS and Linux.
