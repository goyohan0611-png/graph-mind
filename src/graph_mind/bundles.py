"""Work bundles: everything said about one piece of work, in whichever app, as one unit to list,
back up and delete (docs/design/work-bundles.md).

A bundle is a project folder: the turns whose working folder is it or inside it, and the memories
the capture service drew from them. Chats with no folder (desktop apps) are bundled by app as
"app:<client>". Deleting goes through forget.forget_ids, so it reaches the search indexes, the
shared memory and the user's other PCs, and a backup is written first.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import sqlite3

KEEP_BACKUPS = 10


def _norm(path):
    return os.path.normcase(os.path.normpath(path))


def _inside(cwd, root):
    if not cwd:
        return False
    folder = _norm(cwd)
    return folder == root or folder.startswith(root.rstrip("\\/") + os.sep)


def _turns(db, bundle, since=None, until=None):
    """(turn_id, client, happened_at, content) of one bundle."""
    rows = db.execute("SELECT turn_id, client, happened_at, cwd FROM conversation_turns")
    if bundle.startswith("app:"):
        app = bundle[4:]
        picked = [r for r in rows if not r[3] and r[1] == app]
    else:
        root = _norm(bundle)
        picked = [r for r in rows if _inside(r[3], root)]
    return [r for r in picked if (not since or r[2] >= since) and (not until or r[2] < until)]


def _drawn(db, turn_ids):
    """Memories the capture service promoted from these turns."""
    if not turn_ids or not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='semantic_ingestion_audit'").fetchone():
        return []
    wanted = set(turn_ids)
    return sorted({m for t, m in db.execute(
        "SELECT turn_id, memory_id FROM semantic_ingestion_audit WHERE memory_id IS NOT NULL")
        if t in wanted})


def list_bundles(index: Path, limit: int = 30) -> list[dict]:
    """The biggest bundles: folder (or app:<client>), turns, apps, first and last date."""
    db = sqlite3.connect(index, timeout=30)
    try:
        groups = {}
        for cwd, client, at in db.execute(
                "SELECT cwd, client, happened_at FROM conversation_turns"):
            key = _norm(cwd) if cwd else f"app:{client}"
            g = groups.setdefault(key, {"bundle": cwd or key, "turns": 0, "apps": set(),
                                        "first": at, "last": at})
            g["turns"] += 1
            g["apps"].add(client)
            g["first"], g["last"] = min(g["first"], at), max(g["last"], at)
    except sqlite3.OperationalError:
        return []
    finally:
        db.close()
    out = sorted(groups.values(), key=lambda g: -g["turns"])[:limit]
    return [{**g, "apps": sorted(g["apps"])} for g in out]


def plan(index: Path, bundle: str, since=None, until=None) -> dict:
    """What deleting this bundle would remove: counts, the ids and a token that names them."""
    from graph_mind.forget import masked_preview
    db = sqlite3.connect(index, timeout=30)
    try:
        turns = _turns(db, bundle, since, until)
        memories = _drawn(db, [t[0] for t in turns])
        previews = []
        for turn_id, client, at, _ in turns[:3]:
            text = db.execute("SELECT content_redacted FROM conversation_turns WHERE turn_id=?",
                              (turn_id,)).fetchone()
            previews.append({"app": client, "when": at,
                             "preview": masked_preview(text[0] if text else "")})
    finally:
        db.close()
    ids = {"turn_ids": sorted(t[0] for t in turns), "memory_ids": memories}
    token = hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()[:16]
    return {"bundle": bundle, "turns": len(ids["turn_ids"]), "memories": len(memories),
            "apps": sorted({t[1] for t in turns}), "previews": previews, "token": token,
            "ids": ids}


def backup(index: Path, ids: dict, folder: Path, label: str) -> Path:
    """These turns and memories, as an export file `turnbook import` (or restore) reads back.
    The last KEEP_BACKUPS stay."""
    from graph_mind.sharing import _cursor_db, _history, _lines
    wanted = set(ids["turn_ids"]) | set(ids["memory_ids"])
    db = _cursor_db(index)
    db.row_factory = sqlite3.Row
    try:
        items = [(kind, r) for kind, r in _history(db, set())
                 if r.get("turn_id" if kind == "turn" else "memory_id") in wanted]
    finally:
        db.close()
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() else "-" for c in label)[-40:].strip("-") or "bundle"
    target = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{safe}.jsonl"
    target.write_text("".join(line + "\n" for line in _lines(items)), encoding="utf-8")
    for old in sorted(folder.glob("*.jsonl"))[:-KEEP_BACKUPS]:
        old.unlink(missing_ok=True)
    return target


def delete(index: Path, bundle: str, token: str, *, shared=None, backups: Path,
           since=None, until=None) -> dict:
    """Delete a bundle planned with `plan`; refused when it changed since (another token)."""
    from graph_mind.forget import forget_ids
    planned = plan(index, bundle, since, until)
    if planned["token"] != token:
        return {"status": "CHANGED_SINCE_PLANNED", **{k: v for k, v in planned.items()
                                                      if k != "ids"}}
    if not planned["turns"]:
        return {"status": "EMPTY_BUNDLE", "bundle": bundle}
    saved = backup(index, planned["ids"], backups, bundle)
    result = forget_ids(planned["ids"], index, shared)
    return {"status": "DELETED", "bundle": bundle, "backup": str(saved), **result}


def restore(index: Path, source) -> dict:
    """Read a bundle backup back in. Deleting marked its ids as forgotten so nothing brings them
    back; restoring is the user asking for exactly these, so their marks are lifted here first.
    Other PCs keep theirs: the bundle comes back on this PC."""
    from graph_mind.sharing import import_file
    ids = []
    with open(Path(source).expanduser(), encoding="utf-8") as lines:
        for line in lines:
            record = json.loads(line).get("record", {})
            ids.append(record.get("turn_id") or record.get("memory_id"))
    db = sqlite3.connect(index, timeout=30)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS forgotten_ids(id TEXT PRIMARY KEY)")
            db.executemany("DELETE FROM forgotten_ids WHERE id=?", [(i,) for i in ids if i])
    finally:
        db.close()
    return import_file(source, index)
