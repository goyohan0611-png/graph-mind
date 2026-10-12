"""Project brains: shared checkpoints and results, kept apart from personal memory.

Everyone has a private brain (conversations, notes, trial and error), saved automatically on
their own PCs. Every project has a shared brain, and it holds only what someone explicitly
publishes: checkpoints (decision, progress, blocker, next action, artifact, release) and the
results they produced. Nothing in this module reads the conversation or memory stores, so no
misfiled scope can leak a conversation into a project; `source_refs` carry local ids only.

Storage is split. The database holds the ledger: projects, checkpoints, members, the artifact
list with versions, authors, times, hashes and where each version is stored. The files live in a
file store: `hub/artifacts/` on the PC hosting the hub (a NAS path later), or under
~/.graph-mind/hub/artifacts on a PC working alone. Publishing copies the file as it is now; the
working copy is never moved, and editing it later leaves the published version as it was. A
version is visible only once every byte is stored and its metadata is committed, so an upload cut
off halfway never shows up as a file.

Code is not copied: a git folder is linked by repository, branch and commit, and recorded at
the stage it really reached (committed, pushed, pr_open, merged, released), so "done" is never
claimed for a commit that only exists on one PC. GitHub keeps the code history; the ledger keeps
what an AI needs to pick the work up.

Teams. The hub's host invites a teammate (`invite`): Postgres gets a login role for them that
holds no right at all on anyone's personal memory (brain_log) and sees, through row-level
security, only the projects it was given, as editor or viewer. Files are read and written only
through functions on the hub that check the same membership. The teammate's code carries a key
derived one way from the host's share password: it opens project data, never personal lines.
Every action, reads included, is audited; removing a member drops the role.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile

TYPES = ("decision", "progress", "blocker", "next_action", "artifact", "release")
ROLES = ("editor", "viewer")
MAX_FILE = 50 * 2**20            # ponytail: a policy, not a storage limit; raise when needed
CHUNK = 8 * 2**20                # stored in parts: no 50 MB value is ever held whole in a query
TEAM_PREFIX = "gmt1."
TEAM_GROUP = "gm_team"
OWNER_ROLE = "graphmind"
FERNET = b"gAAAAA"               # how every Fernet token begins (version byte 0x80)
BUNDLE_SKIP = {".git", "node_modules", "__pycache__", ".venv", "venv", ".DS_Store"}
CODE_SUFFIXES = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c", ".cpp",
                 ".h", ".cs", ".rb", ".php", ".swift", ".sql", ".sh", ".ps1"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_projects(
    name TEXT PRIMARY KEY, created_by TEXT NOT NULL, created_at TEXT NOT NULL,
    instructions TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS ledger_checkpoints(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, created_at TEXT NOT NULL,
    status TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT {who}, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_artifacts(
    artifact_id TEXT PRIMARY KEY, project TEXT NOT NULL, version INTEGER NOT NULL,
    parts INTEGER NOT NULL, size BIGINT NOT NULL, location TEXT NOT NULL, status TEXT NOT NULL,
    actor_role TEXT NOT NULL DEFAULT {who}, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_access(
    member_role TEXT NOT NULL, project TEXT NOT NULL, role TEXT NOT NULL,
    member TEXT NOT NULL, PRIMARY KEY(member_role, project));
CREATE TABLE IF NOT EXISTS ledger_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT {who},
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL)
"""

# Row-level security on the hub. The owner's role owns the tables and is not bound by these; a
# member role sees and writes only what they allow.
_MEMBER = "SELECT project FROM ledger_access WHERE member_role = current_user"
POLICIES = f"""
ALTER TABLE ledger_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_artifacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE ledger_audit ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS member_read ON ledger_projects;
CREATE POLICY member_read ON ledger_projects FOR SELECT USING (name IN ({_MEMBER}));
DROP POLICY IF EXISTS member_read ON ledger_checkpoints;
CREATE POLICY member_read ON ledger_checkpoints FOR SELECT USING (project IN ({_MEMBER}));
DROP POLICY IF EXISTS member_write ON ledger_checkpoints;
CREATE POLICY member_write ON ledger_checkpoints FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN ({_MEMBER} AND role = 'editor'));
DROP POLICY IF EXISTS member_retract ON ledger_checkpoints;
CREATE POLICY member_retract ON ledger_checkpoints FOR UPDATE
    USING (actor_role = current_user) WITH CHECK (actor_role = current_user);
DROP POLICY IF EXISTS member_read ON ledger_artifacts;
CREATE POLICY member_read ON ledger_artifacts FOR SELECT USING (project IN ({_MEMBER}));
DROP POLICY IF EXISTS member_write ON ledger_artifacts;
CREATE POLICY member_write ON ledger_artifacts FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN ({_MEMBER} AND role = 'editor'));
DROP POLICY IF EXISTS member_own ON ledger_access;
CREATE POLICY member_own ON ledger_access FOR SELECT USING (member_role = current_user);
DROP POLICY IF EXISTS member_log ON ledger_audit;
CREATE POLICY member_log ON ledger_audit FOR INSERT WITH CHECK (actor_role = current_user)
"""

# File store on the hub, reached through these functions only. They run as the database
# superuser (who may read and write files under the hub's folder), check the caller's membership
# themselves (session_user is the caller), and accept ids only, never paths.
HUB_FUNCTIONS = """
CREATE OR REPLACE FUNCTION ledger_can(p text, writing boolean) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $f$
  SELECT session_user = '{owner}' OR EXISTS (SELECT 1 FROM ledger_access
    WHERE member_role = session_user AND project = p AND (NOT writing OR role = 'editor'))
$f$;
CREATE OR REPLACE FUNCTION ledger_store_part(aid text, ver int, part int, p text, data bytea)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
DECLARE target text; o oid;
BEGIN
  IF aid !~ '^af-[0-9a-f]{{12}}$' OR ver < 1 OR part < 0 OR part > 9999 THEN
    RAISE EXCEPTION 'invalid artifact part';
  END IF;
  IF NOT ledger_can(p, true) THEN RAISE EXCEPTION 'permission denied for project %', p; END IF;
  IF EXISTS (SELECT 1 FROM ledger_artifacts WHERE artifact_id = aid) THEN
    RAISE EXCEPTION 'permission denied: artifact % is already published', aid;
  END IF;
  target := '{root}/' || md5(p) || '__' || aid || '__v' || ver;
  o := lo_from_bytea(0, data);
  PERFORM lo_export(o, target || '__p' || lpad(part::text, 4, '0') || '.bin');
  PERFORM lo_unlink(o);
  RETURN target;
END $f$;
CREATE OR REPLACE FUNCTION ledger_read_part(aid text, part int) RETURNS bytea
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
DECLARE a ledger_artifacts;
BEGIN
  SELECT * INTO a FROM ledger_artifacts WHERE artifact_id = aid AND status = 'published';
  IF NOT FOUND OR part < 0 OR part >= a.parts OR NOT ledger_can(a.project, false) THEN
    RAISE EXCEPTION 'artifact not found';
  END IF;
  RETURN pg_read_binary_file(a.location || '__p' || lpad(part::text, 4, '0') || '.bin');
END $f$;
CREATE OR REPLACE FUNCTION ledger_wipe_artifact(aid text) RETURNS int
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
DECLARE a ledger_artifacts; o oid;
BEGIN
  SELECT * INTO a FROM ledger_artifacts WHERE artifact_id = aid;
  IF NOT FOUND THEN RETURN 0; END IF;
  IF session_user <> '{owner}' AND a.actor_role <> session_user THEN
    RAISE EXCEPTION 'permission denied: only who published it, or the owner, can retract it';
  END IF;
  FOR i IN 0 .. a.parts - 1 LOOP
    o := lo_from_bytea(0, ''::bytea);
    PERFORM lo_export(o, a.location || '__p' || lpad(i::text, 4, '0') || '.bin');
    PERFORM lo_unlink(o);
  END LOOP;
  UPDATE ledger_artifacts SET status = 'retracted' WHERE artifact_id = aid;
  RETURN a.parts;
END $f$
"""
HUB_FUNCTION_NAMES = ("ledger_can(text, boolean)",
                      "ledger_store_part(text, int, int, text, bytea)",
                      "ledger_read_part(text, int)", "ledger_wipe_artifact(text)")


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


def _home() -> Path:
    from development_paths import graph_mind_home
    return graph_mind_home()


def project_id(project: str) -> str:
    """A stable folder name for a project: names can be Korean, long, or clash in case."""
    return hashlib.md5(project.encode("utf-8")).hexdigest()


# -- file stores --------------------------------------------------------------------------------
class FolderStore:
    """Results on this PC: hub/artifacts/<project_id>/<artifact_id>/v<N>/<original name>. A
    version is written to a temporary folder and renamed into place whole."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def put(self, project, artifact_id, version, name, pieces) -> tuple[str, int]:
        final = self.root / project_id(project) / artifact_id / f"v{version}"
        partial = final.with_name(f"v{version}.partial-{uuid.uuid4().hex[:8]}")
        partial.mkdir(parents=True)
        parts = 0
        try:
            with (partial / name).open("wb") as stream:
                for piece in pieces:
                    stream.write(piece)
                    parts += 1
            os.replace(partial, final)
        except BaseException:
            shutil.rmtree(partial, ignore_errors=True)
            raise
        return str((final / name).relative_to(self.root)), max(parts, 1)

    def get(self, location, parts):
        with (self.root / location).open("rb") as stream:
            while True:
                piece = stream.read(CHUNK)
                if not piece:
                    break
                yield piece

    def wipe(self, location, artifact_id):
        shutil.rmtree((self.root / location).parent, ignore_errors=True)


class HubStore:
    """Results on the hub's disk, through the hub's file functions (membership checked there)."""

    def __init__(self, ledger):
        self.ledger = ledger

    def put(self, project, artifact_id, version, name, pieces) -> tuple[str, int]:
        location, parts = None, 0
        for part, piece in enumerate(pieces):
            location = self.ledger._sql("SELECT ledger_store_part(?,?,?,?,?)",
                                        (artifact_id, version, part, project,
                                         self.ledger._seal(piece))).fetchall()[0][0]
            parts += 1
        return location, parts

    def get(self, location, parts):
        artifact_id = location.rsplit("__", 2)[-2]
        for part in range(parts):
            yield self.ledger._unseal(self.ledger._sql("SELECT ledger_read_part(?,?)",
                                                       (artifact_id, part)).fetchall()[0][0])

    def wipe(self, location, artifact_id):
        self.ledger._sql("SELECT ledger_wipe_artifact(?)", (artifact_id,))


class Ledger:
    """One project ledger over a DB-API connection and its file store. With `key` (a Fernet),
    summaries and file parts are sealed at rest; a PC's own ledger needs none."""

    def __init__(self, connection, *, postgres=False, key=None, create=True, where="", by="",
                 files: Path | None = None):
        self.db, self.postgres, self.key = connection, postgres, key
        self.where, self.by = where, by
        if create:
            for statement in SCHEMA.format(who="current_user" if postgres else "''").split(";"):
                if statement.strip():
                    self.db.execute(statement)
            self._commit()
        self.store = HubStore(self) if postgres else FolderStore(
            files or _home() / "hub" / "artifacts")

    # -- storage ----------------------------------------------------------------------------
    def _sql(self, query, parameters=()):
        try:
            return self.db.execute(query.replace("?", "%s") if self.postgres else query,
                                   parameters)
        except Exception as error:
            text = str(error)
            if "permission denied" in text or "row-level security" in text:
                raise ValueError("NOT_ALLOWED: your role in this project does not permit "
                                 "that") from None
            if "artifact not found" in text:
                raise ValueError("ARTIFACT_NOT_FOUND: it was retracted, or is not shared with "
                                 "you") from None
            raise

    def _commit(self):
        if not self.postgres:
            self.db.commit()

    @contextlib.contextmanager
    def _atomic(self):
        if self.postgres:
            with self.db.transaction():              # nested: a savepoint
                yield
        else:
            with self.db:                            # sqlite: commit, or roll back on error
                yield

    def _seal(self, data: bytes) -> bytes:
        return self.key.encrypt(data) if self.key else data

    def _unseal(self, data) -> bytes:
        """Sealed data starts as every Fernet token does; anything else was stored before the
        hub had a key, and is read as it is."""
        data = bytes(data)
        return self.key.decrypt(data) if self.key and data.startswith(FERNET) else data

    def _payload(self, stored: str) -> dict:
        return json.loads(self._unseal(stored.encode("utf-8")).decode("utf-8"))

    def _store(self, payload: dict) -> str:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        return self._seal(raw).decode("ascii") if self.key else raw.decode("utf-8")

    def seal_existing(self) -> int:
        """Seal checkpoints stored before the hub had a key: nothing crosses the network in
        clear. (Files are sealed part by part as they are stored.)"""
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
        self._commit()
        return sealed

    def _audit(self, by, action, target):
        self._sql("INSERT INTO ledger_audit(stamp, at, actor, action, target) VALUES(?,?,?,?,?)",
                  (_stamp(), _now(), by, action, str(target)[:200]))

    # -- projects ---------------------------------------------------------------------------
    def create_project(self, name: str, by: str) -> dict:
        name = _project_name(name)
        created = not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (name,)).fetchall()
        if created:
            with self._atomic():
                self._sql("INSERT INTO ledger_projects(name, created_by, created_at) "
                          "VALUES(?,?,?)", (name, by, _now()))
                self._audit(by, "create_project", name)
        return {"project": name, "created": created}

    def names(self) -> list[str]:
        return [row[0] for row in self._sql("SELECT name FROM ledger_projects").fetchall()]

    def projects(self) -> list[dict]:
        out = []
        for name, by, at in self._sql("SELECT name, created_by, created_at FROM ledger_projects "
                                      "ORDER BY name").fetchall():
            last = self._sql("SELECT created_at FROM ledger_checkpoints WHERE project=? AND "
                             "status='published' ORDER BY stamp DESC LIMIT 1", (name,)).fetchall()
            count = self._sql("SELECT COUNT(*) FROM ledger_checkpoints WHERE project=? AND "
                              "status='published'", (name,)).fetchall()[0][0]
            out.append({"project": name, "created_by": by, "created_at": at,
                        "checkpoints": count, "last_shared_at": last[0][0] if last else None})
        return out

    def set_instructions(self, project, text, by) -> dict:
        """The project's standing guidance for every AI that works on it (owner only)."""
        project = self._require(project)
        text = str(text or "").strip()[:4000]
        found = scan_text(text)
        if found:
            raise ValueError("SECRET_IN_INSTRUCTIONS: " + ", ".join(found))
        with self._atomic():
            updated = self._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                                (self._store({"text": text}) if text else "", project))
            if getattr(updated, "rowcount", 1) == 0:
                raise ValueError("NOT_ALLOWED: only the project's owner sets its instructions")
            self._audit(by, "set_instructions", project)
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
                supersedes=None, details=None) -> dict:
        """`summary`: one line. `details`: what changed and why, files and functions, test
        results, links, what is left; a handover note for a teammate, never pasted chat."""
        project = self._require(project)
        checkpoint = self._checkpoint_record(project, kind, summary, by, artifacts, source_refs,
                                             supersedes, details)
        with self._atomic():
            self._insert_checkpoint(checkpoint)
        return {**checkpoint, "status": "published"}

    def _checkpoint_record(self, project, kind, summary, by, artifacts, source_refs, supersedes,
                           details) -> dict:
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
        if any(len(r) > 200 for r in refs):         # ids, hashes and links only: never text
            raise ValueError("SOURCE_REFS_ARE_IDS: give local ids, hashes or links, not text")
        found = scan_text(summary + "\n" + details)
        if found:
            raise ValueError("SECRET_IN_CHECKPOINT: " + ", ".join(found))
        if supersedes:
            self._checkpoint(supersedes, project)
        return {"id": "cp-" + uuid.uuid4().hex[:12], "project": project, "type": kind,
                "summary": summary, "details": details, "actor": by, "created_at": _now(),
                "artifacts": artifacts or [], "source_refs": refs, "supersedes": supersedes}

    def _insert_checkpoint(self, checkpoint):
        self._sql("INSERT INTO ledger_checkpoints(id, project, stamp, created_at, status, payload)"
                  " VALUES(?,?,?,?,?,?)", (checkpoint["id"], checkpoint["project"], _stamp(),
                                           checkpoint["created_at"], "published",
                                           self._store(checkpoint)))
        self._audit(checkpoint["actor"], "publish", f"{checkpoint['project']}/{checkpoint['id']}")

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
            self._commit()
        return out

    def retract(self, checkpoint_id, by) -> dict:
        checkpoint, status = self._checkpoint(checkpoint_id)
        checkpoint.update(retracted_by=by, retracted_at=_now())
        with self._atomic():
            updated = self._sql("UPDATE ledger_checkpoints SET status='retracted', payload=? "
                                "WHERE id=?", (self._store(checkpoint), checkpoint_id))
            if getattr(updated, "rowcount", 1) == 0:     # a member's policy hid another's row
                raise ValueError("NOT_ALLOWED: only the person who published it, or the "
                                 "owner, can retract it")
            self._audit(by, "retract", checkpoint_id)
        removed = 0
        for artifact in checkpoint.get("artifacts", []):
            if artifact.get("artifact_id"):
                self.store.wipe(artifact["location"], artifact["artifact_id"])
                if not self.postgres:
                    with self._atomic():
                        self._sql("UPDATE ledger_artifacts SET status='retracted' WHERE "
                                  "artifact_id=?", (artifact["artifact_id"],))
                removed += 1
        return {"id": checkpoint_id, "status": "retracted", "was": status,
                "files_removed": removed,
                "note": "Removed from the shared project. Copies someone already downloaded "
                        "stay on their PC."}

    # -- results ----------------------------------------------------------------------------
    def attach(self, project, path, summary, by, *, kind="artifact", source_refs=None,
               details=None) -> dict:
        """Publish what the user made: a file (a new version, never an overwrite), a demo
        folder (index.html and what it loads, as one bundle), or for code a link to the git
        commit at the stage it reached."""
        project = self._require(project)
        path = Path(path).expanduser().resolve()
        if path.is_dir():
            if (path / "index.html").is_file():
                return self._publish_file(project, path.name + ".zip", _bundle(path), summary,
                                          by, kind, source_refs, details, bundle=True)
            link = git_link(path)
            if link is None:
                raise ValueError("FOLDER_NOT_UPLOADED: a folder is shared as a demo bundle "
                                 "(it needs an index.html) or, for code, as a git link; "
                                 "otherwise attach its files")
            summary = summary or f"{link['repo']} {link['stage']} @ {link['commit'][:8]}"
            return self.publish(project, kind, summary, by, artifacts=[link],
                                source_refs=source_refs, details=details)
        if not path.is_file():
            raise ValueError(f"FILE_NOT_FOUND: {path}")
        if path.stat().st_size > MAX_FILE:
            raise ValueError(f"FILE_TOO_LARGE: {path.stat().st_size // 2**20} MB; the limit is "
                             f"{MAX_FILE // 2**20} MB. Link where it is stored instead.")
        found = scan_file(path)
        if found:
            raise ValueError("SECRET_IN_FILE: not uploaded. Found " + ", ".join(found)
                             + ". Remove it and try again.")
        data = path.read_bytes()               # the file as it is now: later edits do not touch it
        extra = {}
        commit = git_link(path.parent)
        if commit and path.suffix.lower() in CODE_SUFFIXES:
            extra["git"] = commit
        return self._publish_file(project, path.name, data, summary, by, kind, source_refs,
                                  details, extra=extra)

    def _publish_file(self, project, name, data, summary, by, kind, source_refs, details, *,
                      bundle=False, extra=None) -> dict:
        if len(data) > MAX_FILE:
            raise ValueError(f"FILE_TOO_LARGE: {len(data) // 2**20} MB; the limit is "
                             f"{MAX_FILE // 2**20} MB")
        digest = hashlib.sha256(data).hexdigest()
        with self._atomic():
            if self.postgres:
                # two teammates publishing the same file at once: one waits, then numbers after
                self._sql("SELECT pg_advisory_xact_lock(hashtext(?))", (f"{project}/{name}",))
            known = [a for c in self.timeline(project, limit=10**6, include_withdrawn=True)
                     for a in c.get("artifacts", []) if a.get("name") == name
                     and a.get("artifact_id")]
            live = [a for c in self.timeline(project, limit=10**6)
                    for a in c.get("artifacts", []) if a.get("name") == name
                    and a.get("artifact_id")]
            if live and live[0]["sha256"] == digest:
                return {"status": "UNCHANGED", "name": name, "version": live[0]["version"],
                        "note": "This exact file is already the latest version in the project."}
            version = max((a["version"] for a in known), default=0) + 1
            artifact_id = "af-" + uuid.uuid4().hex[:12]
            # the bytes first; the version exists for anyone only once its row commits below
            location, parts = self.store.put(
                project, artifact_id, version, name,
                (data[start:start + CHUNK] for start in range(0, max(len(data), 1), CHUNK)))
            artifact = {"kind": "bundle" if bundle else "file", "artifact_id": artifact_id,
                        "name": name, "version": version, "sha256": digest, "size": len(data),
                        "parts": parts, "location": location, "created_by": by, **(extra or {})}
            checkpoint = self._checkpoint_record(project, kind, summary or f"{name} v{version}",
                                                 by, [artifact], source_refs, None, details)
            self._sql("INSERT INTO ledger_artifacts(artifact_id, project, version, parts, size, "
                      "location, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (artifact_id, project, version, parts, len(data), location, "published",
                       _now()))
            self._insert_checkpoint(checkpoint)
        return {**checkpoint, "status": "published"}

    def fetch(self, project, *, name=None, version=None, into: Path, by=None) -> dict:
        """The latest (or given) version of a result, written under `into` and hash-checked; a
        demo bundle is unpacked and its index.html returned."""
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
                data = b"".join(self.store.get(artifact["location"], artifact["parts"]))
                if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
                    raise ValueError("ARTIFACT_DAMAGED: the stored file does not match its hash")
                stem, suffix = os.path.splitext(artifact["name"])
                target = Path(into) / _project_name(project) / f"{stem}.v{artifact['version']}{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                opens = target
                if artifact.get("kind") == "bundle":
                    folder = target.with_suffix("")
                    with zipfile.ZipFile(io.BytesIO(data)) as archive:
                        _safe_extract(archive, folder)
                    opens = folder / "index.html"
                if by:
                    self._audit(by, "open", f"{project}/{artifact['name']} v{artifact['version']}")
                    self._commit()
                return {"path": str(opens), "name": artifact["name"],
                        "version": artifact["version"], "checkpoint": checkpoint["id"],
                        "summary": checkpoint["summary"], "by": artifact.get("created_by"),
                        "at": checkpoint["created_at"]}
        raise ValueError("ARTIFACT_NOT_FOUND" + (f": nothing named like '{name}'" if name else "")
                         + (f" at version {version}" if version else ""))

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


def _bundle(folder: Path) -> bytes:
    """A demo folder as one zip: index.html with the CSS, JS and images beside it. Every text file
    in it is checked for secrets first."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if any(part in BUNDLE_SKIP for part in relative.parts) or not path.is_file():
                continue
            found = scan_file(path)
            if found:
                raise ValueError(f"SECRET_IN_FILE: {relative}: " + ", ".join(found))
            archive.write(path, relative.as_posix())
            if buffer.tell() > MAX_FILE:
                raise ValueError(f"FILE_TOO_LARGE: the demo is over {MAX_FILE // 2**20} MB")
    return buffer.getvalue()


def _safe_extract(archive: zipfile.ZipFile, folder: Path):
    folder = folder.resolve()
    for member in archive.infolist():
        target = (folder / member.filename).resolve()
        if folder not in target.parents and target != folder:
            raise ValueError("ARTIFACT_DAMAGED: a bundle entry points outside its folder")
    archive.extractall(folder)


def git_link(folder: Path) -> dict | None:
    """Repository, branch and commit of the git checkout containing `folder`, and the stage the
    commit reached: committed (this PC only), pushed, pr_open, merged, released."""
    def git(*args):
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True,
                              timeout=15, stdin=subprocess.DEVNULL)
        return done.stdout.strip() if done.returncode == 0 else None
    try:
        commit = git("rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        return None
    if not commit:
        return None
    remote = re.sub(r"//[^/@]+@", "//", git("remote", "get-url", "origin") or "")   # no tokens
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    link = {"kind": "git", "repo": remote or Path(git("rev-parse", "--show-toplevel") or folder).name,
            "branch": branch, "commit": commit,
            "uncommitted_changes": bool(git("status", "--porcelain")), "stage": "committed"}
    remote_branches = git("branch", "-r", "--contains", commit) or ""
    if remote_branches:
        link["stage"] = "pushed"
    default = (git("symbolic-ref", "--short", "refs/remotes/origin/HEAD") or "origin/main")
    if remote_branches and default in remote_branches.split():
        link["stage"] = "merged"
    tags = git("tag", "--contains", commit)
    if tags and link["stage"] == "merged":
        link["stage"], link["release"] = "released", tags.splitlines()[0]
    if link["stage"] == "pushed" and shutil.which("gh"):
        try:
            done = subprocess.run(["gh", "pr", "view", branch, "--json", "url,number,state"],
                                  cwd=str(folder), capture_output=True, text=True, timeout=20,
                                  stdin=subprocess.DEVNULL)
            if done.returncode == 0:
                pr = json.loads(done.stdout)
                link["pr"] = {"number": pr["number"], "url": pr["url"]}
                link["stage"] = {"OPEN": "pr_open", "MERGED": "merged"}.get(pr["state"], "pushed")
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            pass                                        # no gh login: pushed is still true
    return link


# -- secret scan --------------------------------------------------------------------------------
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


# -- keys, hub and teams -------------------------------------------------------------------------
def _fernet(secret: bytes):
    from cryptography.fernet import Fernet
    return Fernet(base64.urlsafe_b64encode(secret))


def ledger_secret(share_password: str) -> bytes:
    """The project key, derived one way from the host's share password: teammates get this,
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


def hub_artifacts() -> Path:
    path = _home() / "hub" / "artifacts"
    path.mkdir(parents=True, exist_ok=True)
    return path


_PREPARED = set()


def prepare_hub(owner_url=None) -> None:
    """On the PC hosting a hub: tables, row-level security, the file store's functions and the
    team group. Idempotent; done once per process (brain_log.local_postgres calls it)."""
    import brain_log
    import psycopg
    owner_url = owner_url or brain_log.local_postgres()
    # the file functions name the folder they store in, and each home has its own cluster: a
    # hub prepared for one home is not prepared for another
    key = (owner_url, str(hub_artifacts()))
    if key in _PREPARED:
        return
    _PREPARED.add(key)                       # first: local_postgres() below must not recurse
    try:
        _prepare(owner_url)
    except BaseException:
        _PREPARED.discard(key)               # tried again by the next caller
        raise


def _prepare(owner_url) -> None:
    import brain_log
    import psycopg
    password = brain_log._config().get("share_password")
    with psycopg.connect(owner_url, autocommit=True) as owner:
        # the owner's role owns the tables; what was written before sharing gets sealed now
        Ledger(owner, postgres=True,
               key=_fernet(ledger_secret(password)) if password else None).seal_existing()
        for statement in POLICIES.split(";"):
            if statement.strip():
                owner.execute(statement)
    root = hub_artifacts().as_posix().replace("'", "''")
    with _admin() as admin:
        for statement in HUB_FUNCTIONS.format(root=root, owner=OWNER_ROLE).split("$f$;"):
            if statement.strip():
                admin.execute(statement + ("$f$" if not statement.rstrip().endswith("$f$") else ""))
        for signature in HUB_FUNCTION_NAMES:
            admin.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        if not admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (TEAM_GROUP,)).fetchall():
            admin.execute(f"CREATE ROLE {TEAM_GROUP} NOLOGIN")
        for signature in HUB_FUNCTION_NAMES:
            admin.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO {TEAM_GROUP}, {OWNER_ROLE}")
        admin.execute(f"GRANT CONNECT ON DATABASE graphmind TO {TEAM_GROUP}")
        admin.execute(f"GRANT USAGE ON SCHEMA public TO {TEAM_GROUP}")
        admin.execute(f"GRANT SELECT ON ledger_projects, ledger_checkpoints, ledger_artifacts, "
                      f"ledger_access TO {TEAM_GROUP}")
        admin.execute(f"GRANT INSERT ON ledger_audit TO {TEAM_GROUP}")
        # a hub that hosts only project brains has no personal memory table at all
        if admin.execute("SELECT to_regclass('public.brain_log')").fetchone()[0]:
            admin.execute(f"REVOKE ALL ON brain_log FROM {TEAM_GROUP}")


def secure_hub() -> None:
    """prepare_hub, and let teammates (group gm_team) log in from the network."""
    import brain_log
    prepare_hub()
    hba = brain_log._pg_data() / "pg_hba.conf"
    rule = f"host graphmind +{TEAM_GROUP} all scram-sha-256"
    if rule not in hba.read_text(encoding="utf-8"):
        with hba.open("a", encoding="utf-8") as stream:
            stream.write("\n# Graph-MIND: teammates, project brains only\n" + rule + "\n")
        from pgserver._commands import pg_ctl
        brain_log._retry(lambda: pg_ctl(["reload"], pgdata=brain_log._pg_data(), timeout=60,
                                        **brain_log._DETACHED), attempts=10)


def host_projects() -> dict:
    """Make this PC's hub the home of this person's project brains (a NAS or an always-on PC
    can do it without holding anyone's personal memory). Projects kept on this PC alone so far
    move into it, files included."""
    import brain_log
    opened = brain_log.open_to_network()
    brain_log._save({"hosts_projects": True})
    prepare_hub()
    return {"hosting": True, "moved": _move_local_projects(), "addresses": opened["addresses"],
            "firewall": opened["firewall"]}


def _move_local_projects() -> dict:
    """Projects from ~/.graph-mind/ledger.sqlite into the hub, keeping ids, times and versions."""
    path = _home() / "ledger.sqlite"
    if not path.exists():
        return {"projects": 0, "checkpoints": 0, "files": 0}
    solo = Ledger(sqlite3.connect(path))
    hub = own_ledger()
    moved = {"projects": 0, "checkpoints": 0, "files": 0}
    try:
        for name, by, at, _ in solo._sql("SELECT name, created_by, created_at, instructions "
                                         "FROM ledger_projects").fetchall():
            if name in hub.names():
                continue
            with hub._atomic():
                hub._sql("INSERT INTO ledger_projects(name, created_by, created_at) VALUES(?,?,?)",
                         (name, by, at))
                if solo.instructions(name):
                    hub._sql("UPDATE ledger_projects SET instructions=? WHERE name=?",
                             (hub._store({"text": solo.instructions(name)}), name))
                rows = solo._sql("SELECT id, stamp, created_at, status, payload FROM "
                                 "ledger_checkpoints WHERE project=? ORDER BY stamp",
                                 (name,)).fetchall()
                for row_id, stamp, created, status, payload in rows:
                    checkpoint = solo._payload(payload)
                    for artifact in checkpoint.get("artifacts", []):
                        if not artifact.get("artifact_id"):
                            continue
                        state = solo._sql("SELECT status FROM ledger_artifacts WHERE "
                                          "artifact_id=?", (artifact["artifact_id"],)).fetchall()
                        if status == "retracted" or not state or state[0][0] != "published":
                            continue
                        location, parts = hub.store.put(
                            name, artifact["artifact_id"], artifact["version"], artifact["name"],
                            solo.store.get(artifact["location"], artifact["parts"]))
                        artifact.update(location=location, parts=parts)
                        hub._sql("INSERT INTO ledger_artifacts(artifact_id, project, version, "
                                 "parts, size, location, status, created_at) "
                                 "VALUES(?,?,?,?,?,?,?,?)",
                                 (artifact["artifact_id"], name, artifact["version"], parts,
                                  artifact["size"], location, "published", created))
                        moved["files"] += 1
                    hub._sql("INSERT INTO ledger_checkpoints(id, project, stamp, created_at, "
                             "status, payload) VALUES(?,?,?,?,?,?)",
                             (row_id, name, stamp, created, status, hub._store(checkpoint)))
                    moved["checkpoints"] += 1
            moved["projects"] += 1
    finally:
        solo.db.close()
        hub.db.close()
    return moved


def invite(member: str, role: str, projects: list[str], by: str) -> dict:
    """On the PC hosting the hub: a login for a teammate, limited to `projects`, and the code to
    give them. Projects kept on this PC alone are moved into the hub first."""
    import brain_log
    import secrets
    if role not in ROLES:
        raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
    member = str(member or "").strip()
    if not member or len(member) > 60:
        raise ValueError("MEMBER_NAME: the teammate's name, as others will see it")
    if not projects:
        raise ValueError("PROJECTS_REQUIRED: which projects this teammate may see")
    if not _hosts_here():
        host_projects()
    opened = brain_log.open_to_network()
    secure_hub()
    ledger = own_ledger()
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
            admin.execute(f"GRANT INSERT ON ledger_artifacts TO {login}")
        else:
            admin.execute(f"REVOKE INSERT, UPDATE ON ledger_checkpoints FROM {login}")
            admin.execute(f"REVOKE INSERT ON ledger_artifacts FROM {login}")
        for project in names:
            admin.execute("INSERT INTO ledger_access VALUES(%s,%s,%s,%s) ON CONFLICT "
                          "(member_role, project) DO UPDATE SET role = EXCLUDED.role",
                          (login, project, role, member))
        admin.execute("INSERT INTO ledger_audit(stamp, at, actor_role, actor, action, target) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), OWNER_ROLE, by, "invite",
                                                     f"{member} {role} {','.join(names)}"))
    secret = ledger_secret(brain_log._config()["share_password"])
    code = TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": opened["addresses"], "p": brain_log.PORT, "u": login, "k": password,
         "l": base64.urlsafe_b64encode(secret).decode(), "n": member}).encode()).decode().rstrip("=")
    return {"member": member, "role": role, "projects": names, "team_code": code,
            "firewall": opened["firewall"],
            "note": "Give this code to the teammate privately (it works like a password). On "
                    "their PC they paste it to their AI, which joins the team. It shares only "
                    "these projects: never the gm1. code, which is your personal memory."}


def members() -> list[dict]:
    with _admin() as admin:
        rows = admin.execute("SELECT member, project, role FROM ledger_access "
                             "ORDER BY member, project").fetchall()
    grouped = {}
    for member, project, role in rows:
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
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), OWNER_ROLE, by,
                                                     "remove_member", member))
    return {"member": member, "removed": True,
            "note": "Their access ended now. Files they already downloaded stay on their PC."}


def join_team(code: str) -> dict:
    """On a teammate's PC: add a team hub to the ones it takes part in. It changes nothing about
    this PC's personal memory or where that memory syncs: no conversation is sent anywhere."""
    import brain_log
    value = _decode_team(code)
    key = (tuple(value["h"]), value["p"], value["u"])
    hubs = [c for c in brain_log._config().get("team_hubs", [])
            if (lambda v: (tuple(v["h"]), v["p"], v["u"]))(_decode_team(c)) != key]
    brain_log._save({"team_hubs": hubs + [code.strip()]})
    ledger = _team_ledger(code.strip())
    try:
        visible = ledger.names()
    finally:
        ledger.db.close()
    return {"joined_as": value["n"], "projects": visible, "ledger": ledger.where,
            "personal_memory": "unchanged: still only on your own PCs"}


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


# -- which ledgers ------------------------------------------------------------------------------
class HubUnavailable(Exception):
    """A project hub that cannot be reached: its latest state is unknown, not empty."""


def _team_ledger(code: str) -> Ledger:
    import psycopg
    value = _decode_team(code)
    host = value["h"][0] if value["h"] else "?"
    try:
        connection = psycopg.connect(_team_url(value), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as error:
        raise HubUnavailable(f"team hub at {host}") from error
    return Ledger(connection, postgres=True, key=_fernet(base64.urlsafe_b64decode(value["l"])),
                  create=False, where=f"team hub at {host} (as {value['n']})", by=value["n"])


def _hosts_here() -> bool:
    import brain_log
    settings = brain_log._config()
    return bool(settings.get("hosts_projects")) or settings.get("memory_folder") == "local"


def own_ledger() -> Ledger:
    """This person's own project brains: on the hub this PC hosts or the person's memory uses,
    else on this PC alone (~/.graph-mind/ledger.sqlite, files under hub/artifacts)."""
    import psycopg
    import urllib.parse
    import brain_log
    settings = brain_log._config()
    url = None
    if settings.get("hosts_projects"):
        url = brain_log.local_postgres()
    else:
        shared = brain_log.folder()
        if shared and brain_log.is_database(shared):
            url = shared
    if url:
        if "@127.0.0.1:" in url:
            prepare_hub()
        try:
            connection = psycopg.connect(url, autocommit=True, connect_timeout=5)
        except psycopg.OperationalError as error:
            raise HubUnavailable("your project hub") from error
        password = urllib.parse.urlsplit(url).password
        password = urllib.parse.unquote(password) if password else settings.get("share_password")
        key = _fernet(ledger_secret(password)) if password else None
        return Ledger(connection, postgres=True, key=key, where="your project hub", by=actor())
    path = _home() / "ledger.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(sqlite3.connect(path), by=actor(),
                  where="this PC only (no hub yet: host or join one so others see it)")


def ledgers() -> tuple[list[Ledger], list[str]]:
    """Every project brain this person takes part in, and the hubs that cannot be reached."""
    from brain_log import _config
    found, unreachable = [], []
    try:
        found.append(own_ledger())
    except HubUnavailable as error:
        unreachable.append(str(error))
    for code in _config().get("team_hubs", []):
        try:
            found.append(_team_ledger(code))
        except HubUnavailable as error:
            unreachable.append(str(error))
    return found, unreachable


def ledger_for(project: str) -> Ledger:
    """The one ledger that holds `project`; the others are closed. If it is in none that answers
    and some hub is down, the answer is "unknown", never "no such project"."""
    name = _project_name(project)
    found, unreachable = ledgers()
    chosen = None
    for ledger in found:
        if chosen is None and name in ledger.names():
            chosen = ledger
        else:
            ledger.db.close()
    if chosen is None:
        if unreachable:
            raise HubUnavailable(", ".join(unreachable))
        raise ValueError(f"PROJECT_NOT_FOUND: '{name}' is in none of your project brains")
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
    resolved = str(Path(folder_path).expanduser().resolve())
    bound[resolved] = _project_name(project)
    _save({"project_folders": bound})
    return {"folder": resolved, "project": project}


def downloads() -> Path:
    return _home() / "downloads"


def open_with_default_app(path: str) -> None:
    if os.name == "nt":
        os.startfile(path)                               # noqa: S606 (the user asked to open it)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
