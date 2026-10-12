"""Shared project ledger: checkpoints and their results, kept apart from personal memory.

Personal memory (conversations, notes, trial and error) stays on the user's PCs. What a team
sees is only what someone explicitly publishes here: a checkpoint (decision, progress, blocker,
next action, artifact, release) and the files or commits it produced. Nothing in this module reads
the conversation or memory stores, so no misfiled scope can leak a conversation into a project;
`source_refs` carry local ids only, never text.

Where it lives: the shared hub when this PC uses one (its own tables, sealed), otherwise
~/.graph-mind/ledger.sqlite on this PC alone. Files are versioned, never overwritten: v1, v2, ...
each with its SHA-256, author and time.

Teams. The hub's owner invites a teammate (`invite`): Postgres gets a login role for them that
holds no right at all on the owner's personal memory (brain_log) and sees, through row-level
security, only the projects it was given, as editor (publish, attach, retract its own) or viewer
(read). The teammate's code carries a ledger key derived one way from the owner's share password:
it opens project lines and files, never personal ones. The database enforces all of this, so a
modified client gets no further. Every action, reads included, goes to an audit table only the
owner reads. Removing a member drops the role: access ends at once.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import contextlib
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
ROLES = ("editor", "viewer")
MAX_FILE = 50 * 2**20            # ponytail: a policy, not a Postgres limit; raise when needed
CHUNK = 8 * 2**20                # files go in pieces: no 50 MB value is ever held whole in memory
TEAM_PREFIX = "gmt1."
TEAM_GROUP = "gm_team"
FERNET = b"gAAAAA"                 # how every Fernet token begins (version byte 0x80)
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".cpp",
                 ".h", ".cs", ".rb", ".php", ".swift", ".sql", ".sh", ".ps1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_projects(
    name TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ledger_checkpoints(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT {who}, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_chunks(
    artifact_id TEXT NOT NULL, part INTEGER NOT NULL, project TEXT NOT NULL,
    actor_role TEXT NOT NULL DEFAULT {who}, data {blob} NOT NULL,
    PRIMARY KEY(artifact_id, part));
CREATE TABLE IF NOT EXISTS ledger_access(
    member_role TEXT NOT NULL, project TEXT NOT NULL, role TEXT NOT NULL,
    member TEXT NOT NULL, PRIMARY KEY(member_role, project));
CREATE TABLE IF NOT EXISTS ledger_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT {who},
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL)
"""

# Row-level security on the hub. The owner's role (graphmind) owns the tables and is not bound by
# these; a member role sees and writes only what they allow.
POLICIES = """
ALTER TABLE ledger_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS member_read ON ledger_projects;
CREATE POLICY member_read ON ledger_projects FOR SELECT USING (
    name IN (SELECT project FROM ledger_access WHERE member_role = current_user));
DROP POLICY IF EXISTS member_read ON ledger_checkpoints;
CREATE POLICY member_read ON ledger_checkpoints FOR SELECT USING (
    project IN (SELECT project FROM ledger_access WHERE member_role = current_user));
DROP POLICY IF EXISTS member_write ON ledger_checkpoints;
CREATE POLICY member_write ON ledger_checkpoints FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN (SELECT project FROM ledger_access
        WHERE member_role = current_user AND role = 'editor'));
DROP POLICY IF EXISTS member_retract ON ledger_checkpoints;
CREATE POLICY member_retract ON ledger_checkpoints FOR UPDATE
    USING (actor_role = current_user) WITH CHECK (actor_role = current_user);
DROP POLICY IF EXISTS member_read ON ledger_chunks;
CREATE POLICY member_read ON ledger_chunks FOR SELECT USING (
    project IN (SELECT project FROM ledger_access WHERE member_role = current_user));
DROP POLICY IF EXISTS member_write ON ledger_chunks;
CREATE POLICY member_write ON ledger_chunks FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN (SELECT project FROM ledger_access
        WHERE member_role = current_user AND role = 'editor'));
DROP POLICY IF EXISTS member_remove ON ledger_chunks;
CREATE POLICY member_remove ON ledger_chunks FOR DELETE USING (actor_role = current_user);
DROP POLICY IF EXISTS member_own ON ledger_access;
CREATE POLICY member_own ON ledger_access FOR SELECT USING (member_role = current_user);
DROP POLICY IF EXISTS member_log ON ledger_audit;
CREATE POLICY member_log ON ledger_audit FOR INSERT WITH CHECK (actor_role = current_user)
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
    """Who this person is on their own ledger: config "user_name", else this PC's name. On a
    team hub it is the name in their invite (Ledger.by)."""
    from brain_log import _config, device_name
    return _config().get("user_name") or device_name()


class Ledger:
    """One ledger over a DB-API connection. With `key` (a Fernet), summaries and files are sealed
    at rest; a PC's own file needs none. `where` and `by` say which ledger it is and who acts."""

    def __init__(self, connection, *, postgres=False, key=None, create=True, where="", by=""):
        self.db, self.postgres, self.key = connection, postgres, key
        self.where, self.by = where, by
        if create:
            for statement in SCHEMA.format(blob="BYTEA" if postgres else "BLOB",
                                           who="current_user" if postgres else "''").split(";"):
                if statement.strip():
                    self.db.execute(statement)
            self._commit()

    # -- storage ----------------------------------------------------------------------------
    def _sql(self, query, parameters=()):
        try:
            return self.db.execute(query.replace("?", "%s") if self.postgres else query,
                                   parameters)
        except Exception as error:
            if "permission denied" in str(error) or "row-level security" in str(error):
                if self.postgres:
                    self.db.rollback() if not self.db.autocommit else None
                raise ValueError("NOT_ALLOWED: your role in this project does not permit "
                                 "that") from None
            raise

    def _commit(self):
        if not self.postgres:
            self.db.commit()

    def _seal(self, data: bytes) -> bytes:
        return self.key.encrypt(data) if self.key else data

    def _unseal(self, data) -> bytes:
        """Sealed data starts as every Fernet token does; anything else was stored before the
        hub was shared (or on a PC's own file) and is read as it is."""
        data = bytes(data)
        return self.key.decrypt(data) if self.key and data.startswith(FERNET) else data

    def _payload(self, stored: str) -> dict:
        return json.loads(self._unseal(stored.encode("utf-8")).decode("utf-8"))

    def seal_existing(self) -> int:
        """Seal what was stored before the hub had a key: nothing crosses the network in clear."""
        if not self.key:
            return 0
        sealed = 0
        for row_id, payload in self._sql("SELECT id, payload FROM ledger_checkpoints").fetchall():
            if not payload.startswith(FERNET.decode()):
                self._sql("UPDATE ledger_checkpoints SET payload=? WHERE id=?",
                          (self._seal(payload.encode("utf-8")).decode("ascii"), row_id))
                sealed += 1
        for name, text in self._sql("SELECT name, instructions FROM ledger_projects").fetchall():
            if text and not text.startswith(FERNET.decode()):
                self._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                          (self._seal(text.encode("utf-8")).decode("ascii"), name))
        for artifact_id, part, data in self._sql(
                "SELECT artifact_id, part, data FROM ledger_chunks").fetchall():
            if not bytes(data).startswith(FERNET):
                self._sql("UPDATE ledger_chunks SET data=? WHERE artifact_id=? AND part=?",
                          (self._seal(bytes(data)), artifact_id, part))
        self._commit()
        return sealed

    def _store(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._seal(raw).decode("ascii") if self.key else raw.decode("utf-8")

    def _audit(self, by, action, target):
        self._sql("INSERT INTO ledger_audit(stamp, at, actor, action, target) VALUES(?,?,?,?,?)",
                  (_stamp(), _now(), by, action, str(target)[:200]))

    # -- projects ---------------------------------------------------------------------------
    def create_project(self, name: str, by: str) -> dict:
        name = _project_name(name)
        created = not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (name,)).fetchall()
        if created:
            self._sql("INSERT INTO ledger_projects(name, created_by, created_at) VALUES(?,?,?)",
                      (name, by, _now()))
            self._audit(by, "create_project", name)
            self._commit()
        return {"project": name, "created": created}

    def projects(self) -> list[dict]:
        out = []
        for name, by, at in self._sql("SELECT name, created_by, created_at FROM ledger_projects "
                                      "ORDER BY name").fetchall():
            last = self._sql("SELECT created_at FROM ledger_checkpoints WHERE project=? AND "
                             "status='published' ORDER BY stamp DESC LIMIT 1", (name,)).fetchall()
            count = self._sql("SELECT COUNT(*) FROM ledger_checkpoints WHERE project=? AND "
                              "status='published'", (name,)).fetchall()[0][0]
            out.append({"project": name, "created_by": by, "created_at": at,
                        "checkpoints": count, "last_at": last[0][0] if last else None})
        return out

    def names(self) -> list[str]:
        return [row[0] for row in self._sql("SELECT name FROM ledger_projects").fetchall()]

    def set_instructions(self, project, text, by) -> dict:
        """The project's standing guidance for every AI that works on it (owner only)."""
        project = self._require(project)
        text = str(text or "").strip()[:4000]
        found = scan_text(text)
        if found:
            raise ValueError("SECRET_IN_INSTRUCTIONS: " + ", ".join(found))
        updated = self._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                            (self._store({"text": text}) if text else "", project))
        if getattr(updated, "rowcount", 1) == 0:
            raise ValueError("NOT_ALLOWED: only the project's owner sets its instructions")
        self._audit(by, "set_instructions", project)
        self._commit()
        return {"project": project, "instructions": text}

    def instructions(self, project) -> str:
        rows = self._sql("SELECT instructions FROM ledger_projects WHERE name=?",
                         (_project_name(project),)).fetchall()
        return self._payload(rows[0][0])["text"] if rows and rows[0][0] else ""

    def _require(self, project: str) -> str:
        project = _project_name(project)
        if not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (project,)).fetchall():
            raise ValueError(f"PROJECT_NOT_FOUND: '{project}' does not exist or is not shared "
                             "with you (the owner creates projects and grants access)")
        return project

    # -- checkpoints ------------------------------------------------------------------------
    def publish(self, project, kind, summary, by, *, artifacts=None, source_refs=None,
                supersedes=None, details=None, chunks=()) -> dict:
        """`summary`: one line. `details`: what changed and why, files and functions, test
        results, what is left; written for the teammate who picks it up, never pasted chat."""
        project = self._require(project)
        if kind not in TYPES:
            raise ValueError(f"CHECKPOINT_TYPE must be one of {', '.join(TYPES)}")
        summary = str(summary or "").strip()
        if not summary:
            raise ValueError("CHECKPOINT_SUMMARY_REQUIRED")
        details = str(details or "").strip()
        if len(details) > 4000:
            raise ValueError("DETAILS_TOO_LONG: a checkpoint is a handover note (4,000 "
                             "characters at most), not a transcript")
        refs = [str(r) for r in (source_refs or [])]
        if any(len(r) > 200 for r in refs):         # ids and hashes only: never text
            raise ValueError("SOURCE_REFS_ARE_IDS: give local ids or hashes, not text")
        found = scan_text(summary + "\n" + details)
        if found:
            raise ValueError("SECRET_IN_CHECKPOINT: " + ", ".join(found))
        if supersedes:
            self._checkpoint(supersedes, project)
        checkpoint = {"id": "cp-" + uuid.uuid4().hex[:12], "project": project, "type": kind,
                      "summary": summary, "details": details, "actor": by,
                      "created_at": _now(), "artifacts": artifacts or [], "source_refs": refs,
                      "supersedes": supersedes}
        # all or nothing: a connection lost halfway through a 50 MB upload leaves no orphan chunks
        # and no checkpoint pointing at a half-stored file
        with self._atomic():
            for artifact_id, part, piece in chunks:
                self._sql("INSERT INTO ledger_chunks(artifact_id, part, project, data) "
                          "VALUES(?,?,?,?)", (artifact_id, part, project, self._seal(piece)))
            self._sql("INSERT INTO ledger_checkpoints(id, project, stamp, created_at, status, "
                      "payload) VALUES(?,?,?,?,?,?)",
                      (checkpoint["id"], project, _stamp(), checkpoint["created_at"],
                       "published", self._store(checkpoint)))
            self._audit(by, "publish", f"{project}/{checkpoint['id']}")
        return {**checkpoint, "status": "published"}

    @contextlib.contextmanager
    def _atomic(self):
        if self.postgres:
            with self.db.transaction():              # nested: a savepoint
                yield
        else:
            with self.db:                            # sqlite: commit, or roll back on error
                yield

    def _checkpoint(self, checkpoint_id, project=None) -> tuple[dict, str]:
        rows = self._sql("SELECT project, status, payload FROM ledger_checkpoints WHERE id=?",
                         (checkpoint_id,)).fetchall()
        if not rows or (project and rows[0][0] != project):
            raise ValueError(f"CHECKPOINT_NOT_FOUND: {checkpoint_id}")
        return self._payload(rows[0][2]), rows[0][1]

    def timeline(self, project, *, limit=30, include_withdrawn=False, by=None) -> list[dict]:
        """Newest first. A checkpoint another one supersedes shows as superseded: worked out
        here, so superseding someone's decision never means editing their record."""
        project = self._require(project)
        rows = self._sql("SELECT status, payload FROM ledger_checkpoints WHERE project=? "
                         "ORDER BY stamp DESC", (project,)).fetchall()
        everything = [{**self._payload(payload), "status": status} for status, payload in rows]
        replaced = {c["supersedes"] for c in everything
                    if c.get("supersedes") and c["status"] == "published"}
        out = []
        for checkpoint in everything:
            if checkpoint["status"] == "published" and checkpoint["id"] in replaced:
                checkpoint["status"] = "superseded"
            if checkpoint["status"] == "retracted" and not include_withdrawn:
                continue
            out.append(checkpoint)
            if len(out) >= limit:
                break
        if by:
            self._audit(by, "timeline", project)
        return out

    def retract(self, checkpoint_id, by) -> dict:
        checkpoint, status = self._checkpoint(checkpoint_id)
        checkpoint.update(retracted_by=by, retracted_at=_now())
        with self._atomic():                         # the files go only if the record does
            updated = self._sql("UPDATE ledger_checkpoints SET status='retracted', payload=? "
                                "WHERE id=?", (self._store(checkpoint), checkpoint_id))
            if getattr(updated, "rowcount", 1) == 0:     # a member's policy hid another's row
                raise ValueError("NOT_ALLOWED: only the person who published it, or the "
                                 "owner, can retract it")
            for artifact in checkpoint.get("artifacts", []):
                if artifact.get("artifact_id"):
                    self._sql("DELETE FROM ledger_chunks WHERE artifact_id=?",
                              (artifact["artifact_id"],))
            self._audit(by, "retract", checkpoint_id)
        return {"id": checkpoint_id, "status": "retracted", "was": status,
                "files_removed": sum(bool(a.get("artifact_id"))
                                     for a in checkpoint.get("artifacts", [])),
                "note": "Removed from the shared project. Copies someone already opened stay "
                        "on their PC."}

    # -- artifacts --------------------------------------------------------------------------
    def attach(self, project, path, summary, by, *, kind="artifact", source_refs=None,
               details=None) -> dict:
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
                                by, artifacts=[link], source_refs=source_refs, details=details)
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
        data = path.read_bytes()
        if not self.postgres:
            return self._new_version(project, path, data, summary, by, kind, source_refs, details)
        with self._atomic():
            # two teammates uploading the same file at once: one waits, then numbers after it
            self._sql("SELECT pg_advisory_xact_lock(hashtext(?))", (f"{project}/{path.name}",))
            return self._new_version(project, path, data, summary, by, kind, source_refs, details)

    def _new_version(self, project, path, data, summary, by, kind, source_refs, details):
        size = len(data)
        digest = hashlib.sha256(data).hexdigest()
        versions = [a for c in self.timeline(project, limit=10**6, include_withdrawn=True)
                    for a in c.get("artifacts", []) if a.get("name") == path.name
                    and a.get("artifact_id")]
        live = [a for c in self.timeline(project, limit=10**6) for a in c.get("artifacts", [])
                if a.get("name") == path.name and a.get("artifact_id")]
        if live and live[0]["sha256"] == digest:
            return {"status": "UNCHANGED", "name": path.name, "version": live[0]["version"],
                    "note": "This exact file is already the latest version in the project."}
        version = max((a["version"] for a in versions), default=0) + 1
        artifact_id = "af-" + uuid.uuid4().hex[:12]
        chunks = [(artifact_id, n, data[start:start + CHUNK])
                  for n, start in enumerate(range(0, max(len(data), 1), CHUNK))]
        artifact = {"kind": "file", "artifact_id": artifact_id, "name": path.name,
                    "version": version, "sha256": digest, "size": size, "created_by": by}
        commit = git_link(path.parent)
        if commit and path.suffix.lower() in CODE_SUFFIXES:
            artifact["git"] = commit                # a code file: where it came from, too
        return self.publish(project, kind, summary or f"{path.name} v{version}", by,
                            artifacts=[artifact], source_refs=source_refs, details=details,
                            chunks=chunks)

    def fetch(self, project, *, name=None, version=None, into: Path, by=None) -> dict:
        """The latest (or given) version of an artifact, written under `into`, hash-checked."""
        project = self._require(project)
        wanted = (name or "").casefold()
        for checkpoint in self.timeline(project, limit=10**6):
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
                target = (Path(into) / _project_name(project)
                          / f"{stem}.v{artifact['version']}{suffix}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                if by:
                    self._audit(by, "open", f"{project}/{artifact['name']} v{artifact['version']}")
                    self._commit()
                return {"path": str(target), "name": artifact["name"],
                        "version": artifact["version"], "checkpoint": checkpoint["id"],
                        "summary": checkpoint["summary"], "by": artifact.get("created_by"),
                        "at": checkpoint["created_at"]}
        raise ValueError("ARTIFACT_NOT_FOUND" + (f": nothing named like '{name}'" if name else ""))

    def audit(self, *, limit=50) -> list[dict]:
        rows = self._sql("SELECT at, actor, action, target FROM ledger_audit ORDER BY stamp DESC "
                         "LIMIT ?", (limit,)).fetchall()
        return [{"at": at, "actor": who, "action": action, "target": target}
                for at, who, action, target in rows]


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
                if (info.filename.endswith((".xml", ".txt", ".json", ".csv"))
                        and info.file_size < 20 * 2**20):
                    xml = archive.read(info).decode("utf-8", "replace")
                    texts.append(re.sub(r"<[^>]+>", " ", xml))
        return scan_text("\n".join(texts))
    data = path.read_bytes()
    if b"\0" in data[:8192]:
        return []                                        # binary (an image, a PDF stream)
    return scan_text(data.decode("utf-8", "replace"))


# -- keys and teams ----------------------------------------------------------------------------
def _fernet(secret: bytes):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(secret))


def ledger_secret(share_password: str) -> bytes:
    """The project key, derived one way from the owner's share password: teammates get this,
    and it cannot be turned back into the password or the personal line key."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"graph-mind ledger",
                info=b"ledger key v1").derive(share_password.encode())


def _member_role(member: str) -> str:
    """A Postgres role name for a teammate: ascii, prefixed, unique per display name."""
    slug = re.sub(r"[^a-z0-9]+", "_", member.casefold()).strip("_")[:20] or "member"
    return f"gm_m_{slug}_{hashlib.sha256(member.encode()).hexdigest()[:6]}"


def _decode_team(code: str) -> dict:
    body = code.strip()[len(TEAM_PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return {"h": [str(h) for h in value["h"]], "p": int(value["p"]), "u": str(value["u"]),
                "k": str(value["k"]), "l": str(value["l"]), "n": str(value["n"])}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("INVALID_TEAM_CODE: a team invite starts with gmt1.") from error


def _admin(database="graphmind"):
    import brain_log
    import psycopg
    return psycopg.connect(f"postgresql://postgres@127.0.0.1:{brain_log.PORT}/{database}",
                           autocommit=True)


def secure_hub() -> None:
    """Tables, the team group and row-level security on this PC's hub (owner only)."""
    import brain_log
    import psycopg
    owner_url = brain_log.local_postgres()
    password = brain_log._config().get("share_password")
    with psycopg.connect(owner_url, autocommit=True) as owner:
        # the owner's role owns the tables; what was written before sharing gets sealed now
        Ledger(owner, postgres=True,
               key=_fernet(ledger_secret(password)) if password else None).seal_existing()
        for statement in POLICIES.split(";"):
            if statement.strip():
                owner.execute(statement)
    with _admin() as admin:
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (TEAM_GROUP,)).fetchall():
            admin.execute(f"CREATE ROLE {TEAM_GROUP} NOLOGIN")
        admin.execute(f"GRANT CONNECT ON DATABASE graphmind TO {TEAM_GROUP}")
        admin.execute(f"GRANT USAGE ON SCHEMA public TO {TEAM_GROUP}")
        admin.execute(f"GRANT SELECT ON ledger_projects, ledger_checkpoints, ledger_chunks, "
                      f"ledger_access TO {TEAM_GROUP}")
        admin.execute(f"GRANT INSERT ON ledger_audit TO {TEAM_GROUP}")
        admin.execute(f"REVOKE ALL ON brain_log FROM {TEAM_GROUP}")
    hba = brain_log._pg_data() / "pg_hba.conf"
    rule = f"host graphmind +{TEAM_GROUP} all scram-sha-256"
    if rule not in hba.read_text(encoding="utf-8"):
        with hba.open("a", encoding="utf-8") as stream:
            stream.write("\n# Graph-MIND: teammates, project ledger only\n" + rule + "\n")
        from pgserver._commands import pg_ctl
        brain_log._retry(lambda: pg_ctl(["reload"], pgdata=brain_log._pg_data(), timeout=60,
                                        **brain_log._DETACHED), attempts=10)


def invite(member: str, role: str, projects: list[str], by: str) -> dict:
    """On the hub's PC: a login for a teammate, limited to `projects`, and the code to give them."""
    import brain_log
    import secrets
    if role not in ROLES:
        raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
    member = str(member or "").strip()
    if not member or len(member) > 60:
        raise ValueError("MEMBER_NAME: the teammate's name, as others will see it")
    if not projects:
        raise ValueError("PROJECTS_REQUIRED: which projects this teammate may see")
    if brain_log._config().get("memory_folder") != "local":
        raise ValueError("NOT_THE_HUB: invite from the PC that hosts the shared hub")
    shared = brain_log.share()                           # reachable from the network, password set
    secure_hub()
    ledger, _ = open_ledger()
    try:
        names = [ledger._require(p) for p in projects]
    finally:
        ledger.db.close()
    login, password = _member_role(member), secrets.token_urlsafe(18)
    with _admin() as admin:
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall():
            admin.execute(f"ALTER ROLE {login} LOGIN PASSWORD '{password}'")
        else:
            admin.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{password}' IN ROLE {TEAM_GROUP}")
        if role == "editor":
            admin.execute(f"GRANT INSERT, UPDATE ON ledger_checkpoints TO {login}")
            admin.execute(f"GRANT INSERT, DELETE ON ledger_chunks TO {login}")
        else:
            admin.execute(f"REVOKE INSERT, UPDATE ON ledger_checkpoints FROM {login}")
            admin.execute(f"REVOKE INSERT, DELETE ON ledger_chunks FROM {login}")
        for project in names:
            admin.execute("INSERT INTO ledger_access VALUES(%s,%s,%s,%s) ON CONFLICT "
                          "(member_role, project) DO UPDATE SET role = EXCLUDED.role",
                          (login, project, role, member))
        admin.execute("INSERT INTO ledger_audit(stamp, at, actor_role, actor, action, target) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), "graphmind", by, "invite",
                                                     f"{member} {role} {','.join(names)}"))
    secret = ledger_secret(brain_log._config()["share_password"])
    code = TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": shared["addresses"], "p": brain_log.PORT, "u": login, "k": password,
         "l": base64.urlsafe_b64encode(secret).decode(), "n": member}).encode()).decode().rstrip("=")
    return {"member": member, "role": role, "projects": names, "team_code": code,
            "firewall": shared["firewall"],
            "note": "Give this code to the teammate privately (it works like a password). On "
                    "their PC they paste it to their AI, which joins the team."}


def members() -> list[dict]:
    with _admin() as admin:
        rows = admin.execute("SELECT member, member_role, project, role FROM ledger_access "
                             "ORDER BY member, project").fetchall()
    grouped = {}
    for member, login, project, role in rows:
        grouped.setdefault(member, {"member": member, "projects": {}})["projects"][project] = role
    return list(grouped.values())


def remove_member(member: str, by: str) -> dict:
    login = _member_role(member)
    with _admin() as admin:
        admin.execute("DELETE FROM ledger_access WHERE member_role=%s", (login,))
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall():
            admin.execute(f"DROP OWNED BY {login}")
            admin.execute(f"DROP ROLE {login}")
        admin.execute("INSERT INTO ledger_audit(stamp, at, actor_role, actor, action, target) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), "graphmind", by,
                                                     "remove_member", member))
    return {"member": member, "removed": True,
            "note": "Their access ended now. Files they already opened stay on their PC."}


def join_team(code: str) -> dict:
    """On a teammate's PC: add a team hub to the ones it takes part in (personal memory is
    untouched). One person can be in several: each project brain stays separate."""
    import brain_log
    value = _decode_team(code)
    same = lambda c: (lambda v: (v["h"], v["p"], v["u"]))(_decode_team(c))  # noqa: E731
    hubs = [c for c in brain_log._config().get("team_hubs", [])
            if same(c) != (value["h"], value["p"], value["u"])]
    brain_log._save({"team_hubs": hubs + [code.strip()]})
    ledger = _team_ledger(code.strip())
    try:
        visible = ledger.names()
    finally:
        ledger.db.close()
    return {"joined_as": value["n"], "projects": visible, "ledger": ledger.where}


def leave_team(project: str) -> dict:
    """Stop using the team hub that holds `project` on this PC."""
    import brain_log
    hubs = brain_log._config().get("team_hubs", [])
    for code in hubs:
        ledger = _team_ledger(code)
        try:
            if _project_name(project) in ledger.names():
                brain_log._save({"team_hubs": [c for c in hubs if c != code]})
                return {"left": ledger.where}
        finally:
            ledger.db.close()
    raise ValueError(f"PROJECT_NOT_FOUND: no joined team hub holds '{project}'")


def _team_url(value: dict) -> str:
    import socket
    import urllib.parse
    host = value["h"][0] if value["h"] else "127.0.0.1"
    for candidate in value["h"]:
        try:
            socket.create_connection((candidate, value["p"]), timeout=1.5).close()
            host = candidate
            break
        except OSError:
            continue
    return (f"postgresql://{value['u']}:{urllib.parse.quote(value['k'], safe='')}"
            f"@{host}:{value['p']}/graphmind")


# -- where the ledgers are -------------------------------------------------------------------
def _team_ledger(code: str) -> Ledger:
    import psycopg
    value = _decode_team(code)
    connection = psycopg.connect(_team_url(value), autocommit=True, connect_timeout=5)
    host = value["h"][0] if value["h"] else "?"
    return Ledger(connection, postgres=True, key=_fernet(base64.urlsafe_b64decode(value["l"])),
                  create=False, where=f"team hub at {host} (as {value['n']})", by=value["n"])


def own_ledger() -> Ledger:
    """This person's own ledger: on their shared hub when they use one, else a file here."""
    import psycopg
    from brain_log import _config, folder, is_database
    shared = folder()
    if shared and is_database(shared):
        import urllib.parse
        connection = psycopg.connect(shared, autocommit=True)
        password = urllib.parse.urlsplit(shared).password
        password = urllib.parse.unquote(password) if password else _config().get("share_password")
        key = _fernet(ledger_secret(password)) if password else None
        return Ledger(connection, postgres=True, key=key, where="your shared hub", by=actor())
    from development_paths import graph_mind_home
    path = graph_mind_home() / "ledger.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(sqlite3.connect(path), by=actor(),
                  where="this PC only (no shared hub: share or join one so others see it)")


def ledgers() -> list[Ledger]:
    """Every project brain this person takes part in: their own, then each team hub joined.
    A team hub that cannot be reached right now is left out (and reported by the caller)."""
    from brain_log import _config
    found, unreachable = [own_ledger()], []
    for code in _config().get("team_hubs", []):
        try:
            found.append(_team_ledger(code))
        except Exception:
            unreachable.append(_decode_team(code)["h"][:1])
    ledgers.unreachable = unreachable
    return found


def ledger_for(project: str) -> Ledger:
    """The one ledger that holds `project`; the others are closed."""
    name = _project_name(project)
    chosen = None
    for ledger in ledgers():
        if chosen is None and name in ledger.names():
            chosen = ledger
        else:
            ledger.db.close()
    if chosen is None:
        down = ", ".join(str(h) for h in getattr(ledgers, "unreachable", []))
        raise ValueError(f"PROJECT_NOT_FOUND: '{name}' is in none of your project brains"
                         + (f" (unreachable now: {down})" if down else ""))
    return chosen


def folder_project(cwd=None) -> str | None:
    """The project this working folder is bound to (config project_folders), if any: the
    default when the user says "공유해" without naming one. Sharing still needs the request."""
    from brain_log import _config
    here = Path(cwd or os.getcwd()).resolve()
    best = None
    for folder_path, project in _config().get("project_folders", {}).items():
        root = Path(folder_path)
        if (here == root or root in here.parents) and (
                best is None or len(str(root)) > len(str(best[0]))):
            best = (root, project)
    return best[1] if best else None


def bind_folder(folder_path, project) -> dict:
    from brain_log import _config, _save
    bound = dict(_config().get("project_folders", {}))
    bound[str(Path(folder_path).expanduser().resolve())] = _project_name(project)
    _save({"project_folders": bound})
    return {"folder": str(Path(folder_path).expanduser().resolve()), "project": project}


def open_ledger() -> tuple[Ledger, str]:
    """This person's own ledger (kept for callers that want only it)."""
    ledger = own_ledger()
    return ledger, ledger.where


def open_with_default_app(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)                               # noqa: S606 (the user asked to open it)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
