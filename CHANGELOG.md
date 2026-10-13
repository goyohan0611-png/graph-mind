# Changelog

Upgrade: `python -m pip install --upgrade graph-mind-memory`, then `graph-mind-install`.
Memory is kept across upgrades (checked in CI on Windows, Linux and macOS).

## Unreleased (branch `checkpoints`)
- **Project brains.** `brain_checkpoint`: shared projects apart from personal memory, with
  checkpoints (decision, progress, blocker, next action, artifact, release), handover details,
  supersession and retraction; files as versions (SHA-256, secret scan, 50 MB, sealed chunks);
  git commits linked, not copied; per-project AI instructions; a folder bound to a project.
- **Teams.** Invite codes (`gmt1.`) give each teammate their own Postgres login with no right on
  the owner's personal memory, row-level security per project, editor or viewer roles, a
  ledger-only key, an audit log and immediate removal. One person can join several team hubs.
- Uploads and retractions are all-or-nothing; two uploads of the same file at once get two
  versions.
- **Storage split**: the ledger in the database, results as versioned copies in a file store
  (`hub/artifacts/`), reached on a hub only through membership-checking functions; demo folders
  travel as bundles; a dedicated hub (`host`) holds project brains without personal memory.
- **Reviews**: work context on checkpoints (publisher's account vs. linked record, "not
  recorded" when absent), review requests per exact version with dedupe, opening shows changes
  measured from the files plus history, decisions only on the person's word and for that
  version, PC notifications delivered once, `compare` between versions, git stages.
- An unreachable hub is reported as "latest state unknown", never as an empty project.
- **Owners.** Whoever creates a project owns it: only owners add members (owner, editor, viewer),
  change roles, set instructions and retract others' checkpoints, and a project always keeps an
  owner. Running a hub no longer means seeing its projects: row-level security now binds the
  hosting person's own role too. Teammates can create projects on a team hub. Existing hubs: their
  projects become the hosting person's, who made them.
- Reviews are held to their rules on the hub itself, not only in Python: the requester can only
  cancel, the reviewer can only open and decide, a closed review stays closed, and nobody changes
  who reviews or which version.
- **Inbox** (`graph-mind-inbox`, Start menu "Graph-MIND 요청함", or "요청함 열어줘"): one page on
  this PC with the reviews waiting for you, the answers to yours and each project's latest
  checkpoints. A review opens with the exact version, the changes measured from the files and the
  publisher's account; the file opens in its own app; approve or ask for changes from there.
  Clicking a notification opens its review. Served by the capture service on 127.0.0.1 only, with
  a per-install token.
- **Names people choose.** Teammates see a name the person chose, not a PC's name: the assistant
  asks for it before creating or sharing a project and when joining with an invite
  (`set_name`, `join_team` + `name`, `graph-mind-install --name`). Renaming applies on every hub
  at once; a name is unique per hub; owners add people by the name they chose.
- **Notifications in real time**: each PC listens to its hubs (Postgres LISTEN/NOTIFY); a review
  request or answer shows within seconds instead of at the next 2-minute check, which stays as a
  fallback. The signal carries nothing; what is new is read with the usual permission checks.
- A PC notification that failed to show is tried again on the next round instead of being
  recorded as delivered.
- `graph-mind-hub` for a server or NAS with no AI on it: `host`, `add-user`, `users`,
  `remove-user --transfer-to`, `audit`.
- **Hub connections are TLS.** Every code carries the hub's own certificate (made once,
  self-signed) and PCs check it (`verify-ca`); the hub accepts other machines over TLS only.
  Codes from before still connect, encrypted but unchecked; a new code adds the check.
- **Postgres from its own builds instead of pgserver**, which has no TLS and no ARM build:
  Postgres 16.14 (EnterpriseDB's on Windows, theseus-rs elsewhere), fetched once per machine on
  first hosting, checked against pinned hashes and kept in the user's cache folder. Hosting now
  works on any Python and on 64-bit ARM Linux (a NAS, a Raspberry Pi); `pip install` no longer
  carries a Postgres for people who never host. A hub running on the old programs is restarted on
  the new ones once; its data is kept (same major version).
- Postgres no longer opens console windows on Windows: it runs with a hidden console instead of
  none, so the cmd.exe it starts for its own checks has no window to open either. The random
  start failures: on the PC where they happened, Windows itself refused to start cmd.exe in
  10-30% of plain `cmd /c ver` runs (other programs always started); Postgres needs cmd.exe, so
  the retries stay.

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
