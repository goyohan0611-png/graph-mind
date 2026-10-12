# Changelog

Upgrade: `python -m pip install --upgrade graph-mind-memory`, then `graph-mind-install`.
Memory is kept across upgrades (checked in CI on Windows, Linux and macOS).

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
