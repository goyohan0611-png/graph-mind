# Project brains (optional, for teams)

Graph-MIND is a personal memory first. Teams that want it can add project brains: everyone keeps a
private brain, and every project they choose to share has a shared one. Nothing personal moves into
a project by itself, only what someone publishes. Files and the talk about them stay in Slack, a
drive or GitHub; the project brain is what lets another person's AI pick the work up.

> A: *"Postgres 문제 수정 끝냈어. 공유해."* → a checkpoint: what changed and why, files, tests, the
> commit at the stage it reached, what is left.
> A: *"제안서 슬랙에 올렸어. 프로젝트에도 남겨줘."* → a checkpoint with the Slack link.
> B, next day, in another app: *"어제 Postgres 문제 어떻게 됐고 남은 게 뭐야?"*

- **Checkpoints**: decision, progress, blocker, next action, artifact, release, with their context
  (what changed, where, why, how it was verified, what is left). Later ones can supersede them; a
  mistaken one can be retracted.
- **Links, not files**: a git folder is linked as its commit with the stage it really reached
  (committed, pushed, PR open, merged, released), so a commit on one PC is never reported as done;
  a Slack message or a drive document is kept as its link.
- **Instructions**: standing guidance for every AI that works on the project.
- **Members, as on GitHub**: whoever creates a project owns it. Owners add people (owner, editor or
  viewer), set the instructions and can retract anyone's checkpoint; editors publish; viewers read.
  *"민지를 graph-mind 프로젝트 편집자로 초대해"* gives a code for her; her AI joins with it. Every
  action, reads included, is audited.
- When a project's hub cannot be reached, the assistant says the latest state is unknown instead of
  guessing.

## Where projects live

**On a Postgres hub**: a PC, server or NAS. Each project is its own schema with its own groups and
key, in a database apart from anyone's personal memory, so it can be backed up, moved or removed
whole; the database itself enforces each project's permissions, and teammates' logins cannot reach
anyone's personal memory. Whoever runs the hub runs the server and sees a project only when its
owner adds them. Teammates reach it on the office network, or from anywhere over
[Tailscale](https://tailscale.com) (install it on the hub and on each PC, same tailnet; invite
codes carry the hub's Tailscale address and use it when the office network is not there).

A server needs no AI and no API key, only Graph-MIND (any Python from 3.10; Windows, macOS, or
Linux on x86-64 or 64-bit ARM, such as a Raspberry Pi or an ARM NAS):

```bash
graph-mind-hub host                # once: this machine becomes a hub (it stays one)
graph-mind-hub add-user 민지        # an account and the code to give her (no projects yet)
graph-mind-hub users               # accounts and their roles; never what is in a project
graph-mind-hub remove-user 민지 --transfer-to 준   # projects only she owned go to 준
graph-mind-hub delete-project 이름 --confirm 이름
graph-mind-hub audit
```

**On this PC alone**: without a hub, projects stay in `~/.graph-mind/ledger.sqlite`.
