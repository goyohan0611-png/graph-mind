# What Graph-MIND captures, and how it is kept

## What is captured

| app | captured |
|---|---|
| Codex: terminal, VS Code, ChatGPT desktop's work mode | every turn |
| Claude Code: terminal, VS Code, Claude desktop's Code tab | every turn |
| Claude desktop: Cowork | every turn |
| Claude desktop: chat | what the model saves with `brain_remember` |
| OpenCode, Cursor, Windsurf, Gemini CLI, VS Code, Cline, LM Studio | what the model saves with `brain_remember` (memory tools connected; their transcripts are not read yet) |

Only what you actually send is captured: the service reads each app's transcript, written after
you press Enter, so a paste you delete before sending never reaches it.

## Secrets are masked before anything is stored

API keys, tokens from GitHub, AWS, Google and Slack, private keys, passwords inside URLs, values
written after `password:` / `api_key=` / `token:`, and any long machine-random string even from a
service no rule knows (by its randomness: 91% of random 20-64 character tokens caught; in 246,750
ordinary chat turns it fired 92 times, nearly all on real ids and tokens). Commit ids, hashes and
UUIDs are kept, since you recall those on purpose. A plain password like `hunter2` has no
recognizable format and is **not** masked.

Keep masking on if you use a cloud model: a recalled memory goes to your model's provider with the
question, so a stored key would leave your PC the first time it is useful. To have Graph-MIND
remember a key, tell it **where the key is**: *"my OpenAI key is in 1Password, under Dev"*. If your
model runs on your own machines and you want keys remembered as typed, add
`"mask_secrets": false` to `~/.graph-mind/config.json`.

## Deleting something

Ask your AI: *"delete the password I typed earlier"*. It lists masked candidates
(`wifi password is ****`), deletes only the ones you pick, and the conversation about deleting is
not saved. Or from a terminal: `graph-mind-forget` (asks for the phrase without showing it).

What you pick is deleted from this PC and its search indexes, from the shared memory, and from
your other PCs on their next sync; only ids are shared for that, never the text. Your AI app's own
history (for Claude Code, `~/.claude/projects`) is separate and is not touched.

## Sharing between your PCs

The memory lives in a Postgres server on one of your PCs, which Graph-MIND sets up itself. Your
other PCs log in with a generated password, as a role that can reach only the memory database,
over TLS only: the code carries the hub's own certificate, so a PC accepts that hub and nothing in
between. The Windows firewall rule admits only the local network and Tailscale. Every line is also
encrypted (AES with an HMAC) under a key derived from that password before it leaves a PC, so the
hub's database holds only ciphertext. The code contains the password: do not post it.

The own-PC code shares your **whole** personal memory: never give it to a teammate; invite them to
a [project brain](teams.md) instead.

## Seeing your memory

`graph-mind-view` opens your memory as a 3D graph in the browser (a local file,
`~/.graph-mind/brain.html`; only the 3D library is fetched from a CDN). Use `--no-text` before you
screenshot or share it.

Vulnerabilities: report privately (GitHub **Security → Report a vulnerability**); see
[SECURITY.md](../SECURITY.md).
