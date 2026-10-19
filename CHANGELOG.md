# Changelog

Upgrade: `python -m pip install --upgrade graph-mind-memory`, then `graph-mind-install`.
Memory is kept across upgrades (checked in CI on Windows, Linux and macOS).

## Unreleased
- **"Delete the password I typed" is no longer kept when asked in Gemini CLI or OpenCode.** The
  conversation about deleting is meant to go too (docs/privacy.md), but their hook and plugin
  recorded only text, so the brain_forget call was invisible and the request, secret and all,
  was stored. A Gemini CLI AfterTool hook (brain_forget only) and an OpenCode tool hook now mark
  the call as Claude Code's transcript does. Checked with Gemini CLI 0.64.0: remember, then
  forget; the forget exchange is gone from what is captured.
- docs/privacy.md and docs/install.md list what each app now has captured (Copilot CLI, Cursor's
  CLI agent, Gemini CLI, OpenCode); the privacy page still said their transcripts were not read.

## 0.9.5 — 2026-10-19
- **Gemini CLI hooks work when the Windows profile folder has a space** ("C:\Users\Jane Doe"):
  Gemini runs hooks through PowerShell, where the quoted path alone was a string and the hook
  failed silently, saving nothing. Now `& "path" -m graph_mind.hook ...`.
- **A project with its own `graph_mind.py` or `graph_mind/` no longer breaks the memory there.**
  Apps start the server and hooks in the user's project folder, and `python -m` looked there
  first. `PYTHONSAFEPATH=1` (Python 3.11+) is now set for the server, the hooks and the
  OpenCode plugin.
- **One app's settings can no longer stop the whole install.** A Gemini CLI settings file with
  `"hooks": {"enabled": true}` crashed it, and so would a file an app holds open; that app is now
  skipped with a message and the rest are connected.

## 0.9.4 — 2026-10-19
- Gemini CLI and Copilot CLI are told their conversations are already saved, so the model no
  longer stores the same thing again with brain_remember. (Cursor was too in 0.9.4; 0.9.5 takes
  it back until its editor is seen writing the transcript the CLI does.)
- Cursor turns are dated by the time in the transcript, not by when the capture service read
  them.
- A Gemini CLI reply reported twice by its AfterAgent hook is stored once.
- Hook transcripts keep their read position in their own file, apart from any transcript source
  of the same agent.

## 0.9.3 — 2026-10-18
- Package and registry descriptions name the agents it works with, so a search for "Claude Code
  memory" or "Gemini CLI memory" can find it. No code changes.
- README and docs/benchmarks.md: agentmemory on the same 499 LongMemEval questions, its own
  published retrieval through the same answer model and judge: 80.4% against Graph-MIND's 89.0%,
  reading about 5x more text per question.

## 0.9.2 — 2026-10-18
- **Gemini CLI, Copilot CLI and Cursor conversations are saved too**, so what is said there
  reaches the other agents without the model calling brain_remember. Each was tried for real
  on Windows (Gemini CLI 0.64.0, Copilot CLI 1.0.98, Cursor's CLI agent): a fact said in one
  session was stored, both sides, and a new Gemini or Copilot session answered it. (Cursor's
  new session could not call any MCP tool on the test PC: another tool's broken hook blocked
  them.)
  - Copilot CLI and Cursor: their own transcripts are read (`~/.copilot/session-state`,
    `~/.cursor/projects/*/agent-transcripts`). Cursor's prompt and reply hooks do not run in
    its CLI, and Copilot's never carry the reply.
  - Gemini CLI keeps no transcript to read: `graph-mind-install` adds hooks
    (`python -m graph_mind.hook`) beside the user's own, and `graph-mind-uninstall` removes
    only ours. Its BeforeAgent hook also adds the memory that bears on each message before the
    model answers (as OpenCode's plugin does): the new session answered without a tool call.
- Copilot CLI is connected to the memory server (`~/.copilot/mcp-config.json`).

## 0.9.1 — 2026-10-18
- **OpenCode conversations are saved, and every message brings its memory.** The installer adds
  a plugin: it records what is said for the capture service (stored like Claude Code's turns,
  searchable by meaning, shared with the other agents), and before the model answers a message
  it adds the memory that bears on it (`python -m graph_mind.inject`: 8 items, 4,000
  characters, nothing when the message shows no sign of the past). The model no longer has to
  think of calling brain_context: a free model that never saved the fact and answered "I don't
  have any information about your dog" without the plugin answered "Kongi, a golden retriever"
  with it.
- Looking up the memory for a message takes about 7 s on a cold PC instead of 17.5 s: the MCP
  SDK (6-10 s to import) is now loaded only when the server starts, not by everything that
  borrows its search. Past the plugin's 15 s limit the memory had never reached the model.
- `bench/evidence.py` also reports recall_any@5 and @10, the retrieval number other memory
  projects publish for LongMemEval, so the two can be compared on the same terms.

## 0.9.0 — 2026-10-16
- **OpenCode.** `graph-mind-install` now connects OpenCode too, so any model OpenCode runs that
  can call tools shares the same memory (OpenCode's free models were tested: see
  research/opencode_models.py). OpenCode does not pass a server's instructions to the model, so
  they go in an instructions file its config lists. Its conversations are not captured yet: the
  model saves what it hears with brain_remember.
- **Search by meaning could be quietly wrong; now it checks and repairs itself.** The vectors
  cache shared by the capture service and every app's memory server could pair texts with other
  texts' vectors when two processes saved at the same moment (seen on Linux; on Windows the save
  crashed instead). The capture service now re-embeds a few cached pieces when it starts and,
  if they differ, clears the cache for every process and fills it again. The benchmark's own
  cache had this, so the evidence numbers measured in CI since 0.8.0 were too low.
- **Saved memories are found by meaning too.** "What breed is my dog?" now finds "our golden
  retriever Kongi", and a Korean question an English memory; before, a memory needed a shared
  word. Meaning is used when no word matches (the local model scores short Korean memories as
  all alike, so mixed into word matches it only added noise), and then a fresh server waits up
  to 10 s for the model instead of 0.5 s. This matters most in apps whose conversations are not
  captured (OpenCode): there, what the model saved is all there is.
- **Move a whole memory as one file.** Ask "export my memory to a file" on one PC and "import
  this memory file" on another: `brain_folder(export_to=...)` / `brain_folder(import_from=...)`.
  Importing twice adds nothing. Sharing an existing memory (`share_existing`) also stopped
  dropping a memory's profile, so a fact stays a fact on the other PCs.
- **Tools that are easier to tell apart.** `brain_context` now says it is the default way to
  answer from the past and `brain_recall` that it is for raw rows, ids and one thing's history;
  `brain_folder` (your own memory and PCs) and `brain_checkpoint` (team projects) point at each
  other. A Glama review found the old descriptions near-identical.
- **Facts that change.** `brain_remember` takes `about` and `attribute` ("Kongi" + "breed", "me" +
  "home_city"). A later value of the same fact replaces the earlier one in recall, with no memory
  id needed, and the packet shows the value now and what it was before. Order is when each became
  true, so a fact recorded late from an old conversation stays history. The models are asked to
  record such facts as they hear them, and to record the fact behind an answer that came from old
  conversation turns. Memories take at most a quarter of the packet before conversation turns
  (uncapped, many short facts pushed out the turns that answered), and an `attribute` never
  holds the value ("last_trip", not "last_trip_paris"). Measured on 108 knowledge-update and
  preference questions: 96 with facts, 96 without, so facts do not cost accuracy.
- **The capture service comes back by itself.** It started only at login, so a crash or a kill
  meant nothing was saved until the next login. Now the memory server of every app that relies on
  it checks once a minute and starts it again; a second one never starts beside it.
- **One model for every app.** The capture service, running since login with the embedding model
  loaded, lends it to the memory servers (127.0.0.1, keyed). An app's server no longer loads its
  own: search by meaning works from its first question and each app's server holds ~90 MB instead
  of ~830 MB (`bench/latency.py`, 2,000 turns: first answer 9.7 s -> 0.8 s). Without the service
  running, a server loads the model itself as before.
- `bench/evidence.py` embeds each haystack before asking, as the capture service does: a backlog
  sent to the background left some questions to word search alone, so two runs of the same code
  disagreed (91.6% / 91.4%). It also no longer keeps every question's index in memory.
- **Measurements** (`bench/`), the yardstick for the structure work ahead: `evidence.py` (does
  the answer reach the packet, all of LongMemEval_S, offline), `latency.py` (server start, first
  answer, when search by meaning works, steady p50/p95, memory), `recovery.py` (every fault meant
  to heal, run and timed, and the ones not handled yet). Weekly in CI (`bench` workflow).
- A hub Postgres that was killed comes back on the next use: its log now lives outside its data
  folder (the crash recovery's disk sync waited 30 s on it) and a start may take up to 5 minutes
  (that sync alone took 25-55 s on a laptop with antivirus).

## 0.8.0 — 2026-10-14
Run `graph-mind-install` after upgrading: it registers the server at its new place. Apps set up
by an earlier version keep working until then. Your memory is kept.
- **One package.** The code is the `graph_mind` package (`python -m graph_mind.mcp_server`)
  instead of twenty top-level modules, which had put names like `install` and `forget` into
  every Python environment it was installed in. Modules that were named for their history now
  say what they do: `sharing`, `project_brains`, `paths`, `context_packet`, `warmup`,
  `memory_view`, `capture_cli`. From a source checkout: `pip install -e .`.
- **Removed what never ran**: the opt-in OpenAI extraction in the capture service (off by
  default, and at odds with saving memory never calling a model), the old development-event
  writer (its records in older memories are still read), and unused recall helpers. Research
  code and its tests live under `research/`.
- **Team hubs**: one account per name, so adding "Mina" can never pick someone who took her
  name; an owner can take a teammate's checkpoint back but not rewrite it; `remove_member`
  without a project no longer closes the whole account (`graph-mind-hub remove-user` does).
- SECURITY.md and the teams page say plainly what the hub machine's own users and its operator
  can read.
- A hub whose first start failed right after it was created (Windows refusing cmd.exe) no
  longer stays without the memory's database: it is created on the next start.
- `ruff check .` passes and runs in CI.

## 0.7.0 — 2026-10-13
Run `graph-mind-install` after upgrading. Every PC that shares a hub should upgrade: the hub moves
to its own Postgres build with TLS, and team logins reach a new hub database. Your memory is kept.
- **Project brains (optional, for teams).** `brain_checkpoint`: shared projects apart from
  personal memory, which stays the product, holding only what someone publishes: checkpoints
  (decision, progress, blocker, next action, artifact, release) with their context (what changed, where, why, verification, what is left, what to
  check; "not recorded" where nothing was written), supersession and retraction; per-project AI
  instructions; a working folder bound to a project. Files and the talk about them stay in
  Slack, drives and GitHub: a git folder is linked as its commit at the stage it really reached
  (committed, pushed, PR open, merged, released) and a Slack or drive address as a link.
- **Postgres hubs** (office network or Tailscale): every project its own schema, groups and
  key in a database apart from personal memory; teammates' logins cannot reach anyone's
  personal memory; row-level security per project; an audit log. Whoever creates
  a project owns it; only owners add members (owner, editor, viewer), set instructions or retract
  others' checkpoints; whoever runs the hub runs the server and sees a project only when added.
  `graph-mind-hub` for a server or NAS with no AI on it: `host`, `add-user`, `users`,
  `remove-user --transfer-to`, `delete-project`, `audit`.
- **One-step install, no Python needed**: `scripts/install.ps1` (or double-click
  `Graph-MIND-Setup.cmd`) on Windows, `scripts/install.sh` on macOS and Linux. They install uv,
  which brings its own Python, then Graph-MIND, then connect the AI apps. Checked in CI on all
  three systems against each release.
- **One kind of code for the user**: a code pasted to the AI works whichever tool receives it,
  whether it is for the user's own other PC or a team invite; `graph-mind-install --join` takes
  either too.
- **Names people choose**: teammates see a chosen name, not a PC's; the assistant asks for it
  before creating or sharing a project and when joining (`set_name`, `install --name`).
- An unreachable hub is reported as "latest state unknown", never as an empty project.
- **Hub connections are TLS**: every code carries the hub's own certificate and PCs check it.
- **Postgres from its own builds instead of pgserver** (no TLS, no ARM): Postgres 16.14 fetched
  once per machine on first hosting, checked against pinned hashes. Hosting works on any Python
  and on 64-bit ARM Linux; `pip install` no longer carries a Postgres for people who never host.
  A hub on the old programs is restarted on the new ones once, data kept. Hubs from before this
  branch are converted on first start (each project into its own schema, sealed again).
- Postgres no longer opens console windows on Windows (a hidden console instead of none). The
  random start failures: on the PC where they happened, Windows refused to start cmd.exe in
  10-30% of plain `cmd /c ver` runs; Postgres needs cmd.exe, so the retries stay.

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
