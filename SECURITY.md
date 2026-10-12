# Security

Graph-MIND stores your conversations with AI assistants, so a flaw here can expose things people
said in private. Reports are welcome and taken seriously.

## Reporting a vulnerability

Please do not open a public issue. Report privately, either:

- on GitHub: **Security → Report a vulnerability** in this repository, or
- by email to goyohan0611@gmail.com, with "Graph-MIND security" in the subject.

Include what you found, how to reproduce it, and what an attacker could do with it. This is a
one-person project: expect a first reply within a few days. A fix ships as a new PyPI release, and
you are credited in its notes unless you prefer not to be.

Only the latest release is supported.

## What is in scope

- secrets reaching the store despite masking (keys, tokens, passwords in a recognisable form)
- anything that sends memory off the machine other than the AI app's own requests
- the shared memory: logging in without the connection code, reaching anything beyond the memory
  database, or reading another PC's memory without it
- `brain_forget` / `graph-mind-forget` leaving deleted text recoverable in Graph-MIND's stores
- the installer or capture service running something it should not

## How the data is handled

- Memory lives in `~/.graph-mind` on each PC: SQLite files, plain text, not encrypted at rest.
  Protect it as you protect the rest of your home folder.
- Capture reads each AI app's local transcript; known secret formats and long machine-random
  strings are masked before storage. Plain passwords without a recognisable form are not.
- When the AI recalls a memory, that text goes to the model provider with your question, as
  anything in the chat does.
- Sharing across PCs uses a Postgres server on one PC, reachable only from the local network and
  Tailscale, with a generated password (SCRAM, never sent in clear) and a role limited to the
  memory database. The Postgres connection itself is not TLS (the bundled server is built without
  SSL), so every memory line is sealed with Fernet (AES-128-CBC + HMAC-SHA256) under a key derived
  from the connection code's password before it is sent: the wire and the hub's tables hold
  ciphertext only. Who holds the code can read the memory.
- There is no telemetry and no Graph-MIND server.
