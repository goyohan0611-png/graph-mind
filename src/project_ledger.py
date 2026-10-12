"""Shared project ledger: checkpoints and their results, kept apart from personal memory.

Personal memory (conversations, notes, trial and error) stays on the user's PCs. What a team
sees is only what someone explicitly publishes here: a checkpoint (decision, progress, blocker,
next action, artifact, release) and the files or commits it produced. Nothing in this module reads
the conversation or memory stores, so no misfiled scope can leak a conversation into a project;
`source_refs` carry local ids only, never text.

Where it lives: the shared hub when this PC uses one (its own tables, lines and file chunks
sealed with the hub's key, as brain_log seals memory lines); otherwise ~/.graph-mind/ledger.sqlite
on this PC alone. Files are versioned, never overwritten: "proposal.pptx" v1, v2, ... each with
its SHA-256, author and time. A retracted checkpoint loses its files on the hub; copies someone
already opened stay on their PC.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

TYPES = ("decision", "progress", "blocker", "next_action", "artifact", "release")
MAX_FILE = 50 * 2**20            # ponytail: a policy, not a Postgres limit; raise when needed
CHUNK = 8 * 2**20                # files go in pieces: no 50 MB value is ever held whole in memory
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".cpp",
                 ".h", ".cs", ".rb", ".php", ".swift", ".sql", ".sh", ".ps1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_projects(
    name TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_checkpoints(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_chunks(
    artifact_id TEXT NOT NULL, part INTEGER NOT NULL, data {blob} NOT NULL,
    PRIMARY KEY(artifact_id, part));
"""


def _stamp() -> str:
    """Order of publishing: created_at is to the second, and two checkpoints often share one.
    Strictly increasing within a process: Windows' clock can return the same ns twice."""
    global _LAST
    _LAST = max(time.time_ns(), _LAST + 1)
    return f"{_LAST:020d}"


_LAST = 0


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def actor() -> str:
    """Who is publishing: config "user_name" (a person's name on a team), else this PC's name."""
    from brain_log import _config, device_name
    return _config().get("user_name") or device_name()


class Ledger:
    """One ledger over a DB-API connection. `seal`/`unseal` protect text and bytes at rest
    (identity on a PC's own file)."""

    def __init__(self, connection, *, postgres=False, key=None):
        self.db, self.postgres, self.key = connection, postgres, key
        for statement in SCHEMA.format(blob="BYTEA" if postgres else "BLOB").split(";"):
            if statement.strip():
                self.db.execute(statement)
        self._commit()

    # -- storage ----------------------------------------------------------------------------
    def _sql(self, query, parameters=()):
        return self.db.execute(query.replace("?", "%s") if self.postgres else query, parameters)

    def _commit(self):
        if not self.postgres:
            self.db.commit()

    def _seal(self, data: bytes) -> bytes:
        return self.key.encrypt(data) if self.key else data

    def _unseal(self, data) -> bytes:
        data = bytes(data)
        return self.key.decrypt(data) if self.key else data

    def _payload(self, row_payload: str) -> dict:
        return json.loads(self._unseal(row_payload.encode("ascii") if self.key else
                                       row_payload.encode("utf-8")).decode("utf-8"))

    def _store(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._seal(raw).decode("ascii") if self.key else raw.decode("utf-8")

    # -- projects ---------------------------------------------------------------------------
    def create_project(self, name: str, by: str) -> dict:
        name = _project_name(name)
        if not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (name,)).fetchall():
            self._sql("INSERT INTO ledger_projects VALUES(?,?,?)", (name, by, _now()))
            self._commit()
            return {"project": name, "created": True}
        return {"project": name, "created": False}

    def projects(self) -> list[dict]:
        rows = self._sql("SELECT name, created_by, created_at FROM ledger_projects ORDER BY name")
        out = []
        for name, by, at in rows.fetchall():
            last = self._sql("SELECT created_at FROM ledger_checkpoints WHERE project=? "
                             "ORDER BY stamp DESC LIMIT 1", (name,)).fetchall()
            count = self._sql("SELECT COUNT(*) FROM ledger_checkpoints WHERE project=? AND "
                              "status='published'", (name,)).fetchall()[0][0]
            out.append({"project": name, "created_by": by, "created_at": at,
                        "checkpoints": count, "last_at": last[0][0] if last else None})
        return out

    def _require(self, project: str) -> str:
        project = _project_name(project)
        if not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (project,)).fetchall():
            raise ValueError(f"PROJECT_NOT_FOUND: create the project '{project}' first "
                             "(the user decides what becomes a shared project)")
        return project

    # -- checkpoints ------------------------------------------------------------------------
    def publish(self, project, kind, summary, by, *, artifacts=None, source_refs=None,
                supersedes=None) -> dict:
        project = self._require(project)
        if kind not in TYPES:
            raise ValueError(f"CHECKPOINT_TYPE must be one of {', '.join(TYPES)}")
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("CHECKPOINT_SUMMARY_REQUIRED")
        refs = [str(r) for r in (source_refs or [])]
        if any(len(r) > 200 for r in refs):         # ids and hashes only: never text
            raise ValueError("SOURCE_REFS_ARE_IDS: give local ids or hashes, not text")
        found = scan_text(summary)
        if found:
            raise ValueError("SECRET_IN_SUMMARY: " + ", ".join(found))
        if supersedes:
            self._checkpoint(supersedes, project)
        checkpoint = {"id": "cp-" + uuid.uuid4().hex[:12], "project": project, "type": kind,
                      "summary": summary, "actor": by, "created_at": _now(),
                      "artifacts": artifacts or [], "source_refs": refs,
                      "supersedes": supersedes}
        self._sql("INSERT INTO ledger_checkpoints VALUES(?,?,?,?,?,?)",
                  (checkpoint["id"], project, _stamp(), checkpoint["created_at"], "published",
                   self._store(checkpoint)))
        if supersedes:
            self._sql("UPDATE ledger_checkpoints SET status='superseded' WHERE id=?", (supersedes,))
        self._commit()
        return {**checkpoint, "status": "published"}

    def _checkpoint(self, checkpoint_id, project=None) -> tuple[dict, str]:
        rows = self._sql("SELECT project, status, payload FROM ledger_checkpoints WHERE id=?",
                         (checkpoint_id,)).fetchall()
        if not rows or (project and rows[0][0] != project):
            raise ValueError(f"CHECKPOINT_NOT_FOUND: {checkpoint_id}")
        return self._payload(rows[0][2]), rows[0][1]

    def timeline(self, project, *, limit=30, include_withdrawn=False) -> list[dict]:
        project = self._require(project)
        rows = self._sql("SELECT status, payload FROM ledger_checkpoints WHERE project=? "
                         "ORDER BY stamp DESC", (project,)).fetchall()
        out = []
        for status, payload in rows:
            if status == "retracted" and not include_withdrawn:
                continue
            out.append({**self._payload(payload), "status": status})
            if len(out) >= limit:
                break
        return out

    def retract(self, checkpoint_id, by) -> dict:
        checkpoint, status = self._checkpoint(checkpoint_id)
        for artifact in checkpoint.get("artifacts", []):
            if artifact.get("artifact_id"):
                self._sql("DELETE FROM ledger_chunks WHERE artifact_id=?", (artifact["artifact_id"],))
        checkpoint.update(retracted_by=by, retracted_at=_now())
        self._sql("UPDATE ledger_checkpoints SET status='retracted', payload=? WHERE id=?",
                  (self._store(checkpoint), checkpoint_id))
        self._commit()
        return {"id": checkpoint_id, "status": "retracted", "was": status,
                "files_removed": sum(bool(a.get("artifact_id"))
                                     for a in checkpoint.get("artifacts", [])),
                "note": "Removed from the shared project. Copies someone already opened stay "
                        "on their PC."}

    # -- artifacts --------------------------------------------------------------------------
    def attach(self, project, path, summary, by, *, kind="artifact", source_refs=None) -> dict:
        """Publish a file (a new version, never an overwrite) or, for a folder that is a git
        repository, a link to its current commit."""
        project = self._require(project)
        path = Path(path).expanduser().resolve()
        if path.is_dir():
            link = git_link(path)
            if link is None:
                raise ValueError("FOLDER_NOT_UPLOADED: folders are not copied; attach the "
                                 "files, or a git repository to link its commit")
            return self.publish(project, kind, summary or f"{link['repo']} @ {link['commit'][:8]}",
                                by, artifacts=[link], source_refs=source_refs)
        if not path.is_file():
            raise ValueError(f"FILE_NOT_FOUND: {path}")
        size = path.stat().st_size
        if size > MAX_FILE:
            raise ValueError(f"FILE_TOO_LARGE: {size // 2**20} MB; the limit is "
                             f"{MAX_FILE // 2**20} MB. Link where it is stored instead.")
        found = scan_file(path)
        if found:
            raise ValueError("SECRET_IN_FILE: not uploaded. Found " + ", ".join(found)
                             + ". Remove it and try again.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        versions = [a for c in self.timeline(project, limit=10**6) for a in c.get("artifacts", [])
                    if a.get("name") == path.name and a.get("artifact_id")]
        if versions and versions[0]["sha256"] == digest:
            return {"status": "UNCHANGED", "name": path.name, "version": versions[0]["version"],
                    "note": "This exact file is already the latest version in the project."}
        version = max((a["version"] for a in versions), default=0) + 1
        artifact_id = "af-" + uuid.uuid4().hex[:12]
        with path.open("rb") as stream:
            part = 0
            while True:
                piece = stream.read(CHUNK)
                if not piece:
                    break
                self._sql("INSERT INTO ledger_chunks VALUES(?,?,?)",
                          (artifact_id, part, self._seal(piece)))
                part += 1
        artifact = {"kind": "file", "artifact_id": artifact_id, "name": path.name,
                    "version": version, "sha256": digest, "size": size, "created_by": by}
        commit = git_link(path.parent)
        if commit and path.suffix.lower() in CODE_SUFFIXES:
            artifact["git"] = commit                # a code file: where it came from, too
        return self.publish(project, kind, summary or f"{path.name} v{version}", by,
                            artifacts=[artifact], source_refs=source_refs)

    def fetch(self, project, *, name=None, version=None, into: Path) -> dict:
        """The latest (or given) version of an artifact, written under `into`, hash-checked."""
        project = self._require(project)
        wanted = (name or "").casefold()
        for checkpoint in self.timeline(project, limit=10**6):
            if checkpoint["status"] == "retracted":
                continue
            for artifact in checkpoint.get("artifacts", []):
                if not artifact.get("artifact_id"):
                    continue
                if wanted and wanted not in artifact["name"].casefold():
                    continue
                if version and artifact["version"] != int(version):
                    continue
                rows = self._sql("SELECT data FROM ledger_chunks WHERE artifact_id=? "
                                 "ORDER BY part", (artifact["artifact_id"],)).fetchall()
                data = b"".join(self._unseal(row[0]) for row in rows)
                if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    raise ValueError("ARTIFACT_DAMAGED: the stored file does not match its hash")
                stem, suffix = os.path.splitext(artifact["name"])
                target = Path(into) / _project_name(project) / f"{stem}.v{artifact['version']}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                return {"path": str(target), "name": artifact["name"],
                        "version": artifact["version"], "checkpoint": checkpoint["id"],
                        "summary": checkpoint["summary"], "by": artifact.get("created_by"),
                        "at": checkpoint["created_at"]}
        raise ValueError("ARTIFACT_NOT_FOUND" + (f": nothing named like '{name}'" if name else ""))


def _project_name(name) -> str:
    name = re.sub(r"\s+", "-", str(name or "").strip())
    if not name or len(name) > 80 or "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("PROJECT_NAME: a short name, e.g. graph-mind or 고객사-A-제안")
    return name


def git_link(folder: Path) -> dict | None:
    """Repository, branch and commit of the git checkout containing `folder`, or None."""
    def git(*args):
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=10, stdin=subprocess.DEVNULL)
        return done.stdout.strip() if done.returncode == 0 else None
    try:
        commit = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if not commit:
        return None
    remote = git("remote", "get-url", "origin") or ""
    remote = re.sub(r"//[^/@]+@", "//", remote)          # never a token inside the URL
    return {"kind": "git", "repo": remote or Path(git("rev-parse", "--show-toplevel") or folder).name,
            "branch": git("rev-parse", "--abbrev-ref", "HEAD"), "commit": commit,
            "uncommitted_changes": bool(git("status", "--porcelain"))}


# -- secret scan ----------------------------------------------------------------------------
def scan_text(text: str) -> list[str]:
    """Kinds of secrets in the text: the masking rules capture uses, applied whatever the
    user's mask_secrets setting (that setting is about this PC; this is about sharing)."""
    from conversation_memory import RANDOM_CANDIDATE, SECRET_PATTERNS, _looks_random
    found = []
    for pattern, replacement in SECRET_PATTERNS:
        if pattern.search(text):
            found.append(re.sub(r"[\\\[\]\d]", "", replacement).strip("_ ") or "secret")
    if any(_looks_random(m.group(0)) for m in RANDOM_CANDIDATE.finditer(text)):
        found.append("random token")
    return sorted(set(found))


def scan_file(path: Path) -> list[str]:
    """Secrets in a file's text: plain text files, and the text inside Office files (docx, pptx,
    xlsx are zip archives of XML). Images and other binaries carry no readable text to check."""
    if zipfile.is_zipfile(path):
        texts = []
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.filename.endswith((".xml", ".txt", ".json", ".csv")) and info.file_size < 20 * 2**20:
                    xml = archive.read(info).decode("utf-8", "replace")
                    texts.append(re.sub(r"<[^>]+>", " ", xml))
        return scan_text("\n".join(texts))
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return []                                        # binary (an image, a PDF stream)
    return scan_text(data.decode("utf-8", "replace"))


# -- where the ledger is ---------------------------------------------------------------------
def open_ledger() -> tuple[Ledger, str]:
    """The ledger for this PC: on the shared hub when it uses one, else this PC's own file."""
    from brain_log import _line_key, folder, is_database
    shared = folder()
    if shared and is_database(shared):
        import psycopg
        connection = psycopg.connect(shared, autocommit=True)
        where = "the shared hub" if "@127.0.0.1:" not in shared else "this PC's shared hub"
        return Ledger(connection, postgres=True, key=_line_key(shared)), where
    from development_paths import graph_mind_home
    path = graph_mind_home() / "ledger.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    note = "this PC only (no shared hub yet: share or join one so others see it)"
    return Ledger(sqlite3.connect(path)), note


def open_with_default_app(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)                               # noqa: S606 (the user asked to open it)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
