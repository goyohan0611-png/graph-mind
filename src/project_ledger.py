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

Teams, as on GitHub. Whoever runs a hub runs the server: on the hub's machine they give a
person an account (`add_account`, or `graph-mind-hub add-user`), a Postgres login that holds no
right at all on anyone's personal memory (brain_log) and, through row-level security forced on
every role, sees only the projects it is a member of. Whoever creates a project owns it, and
only owners add members (owner, editor or viewer), change roles, set the instructions or take
others' checkpoints back; the hosting person's own role is bound by the same rules. Files are
read and written only through functions on the hub that check the same membership. The code a
person gets carries a key derived one way from the host's share password: it opens project
data, never personal lines. Every action, reads included, is audited; closing an account drops
the login. The operator's tools name accounts and roles, never what is in a project (the
machine's administrator can still reach its disk, as with any server).
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
ROLES = ("owner", "editor", "viewer")
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
CREATE TABLE IF NOT EXISTS ledger_reviews(
    id TEXT PRIMARY KEY, project TEXT NOT NULL, stamp TEXT NOT NULL, status TEXT NOT NULL,
    requester_role TEXT NOT NULL DEFAULT {who}, reviewer_role TEXT NOT NULL,
    reviewer TEXT NOT NULL, target TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_audit(
    stamp TEXT NOT NULL, at TEXT NOT NULL, actor_role TEXT NOT NULL DEFAULT {who},
    actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger_accounts(
    member_role TEXT PRIMARY KEY, member TEXT NOT NULL)
"""

# Row-level security on the hub, forced on every role, the tables' owner (the hosting person's
# own role, "graphmind") included: whoever runs the hub runs the server, and sees a project only
# when its owner made them a member. Whoever creates a project owns it. Only the database
# superuser, which logs in from the hub's own machine alone, is above these rules; its tools
# manage accounts and never read project content.
_MEMBER = "SELECT project FROM ledger_access WHERE member_role = current_user"
_WRITER = _MEMBER + " AND role IN ('owner', 'editor')"
_OWNER = _MEMBER + " AND role = 'owner'"
LEDGER_TABLES = ("ledger_projects", "ledger_checkpoints", "ledger_artifacts", "ledger_access",
                 "ledger_audit", "ledger_reviews", "ledger_accounts")
POLICIES = "".join(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY;\n"
                   f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY;\n" for t in LEDGER_TABLES) + f"""
DROP POLICY IF EXISTS member_read ON ledger_reviews;
CREATE POLICY member_read ON ledger_reviews FOR SELECT USING (project IN ({_MEMBER}));
DROP POLICY IF EXISTS member_request ON ledger_reviews;
CREATE POLICY member_request ON ledger_reviews FOR INSERT WITH CHECK (
    requester_role = current_user AND project IN ({_MEMBER})
    AND ledger_is_member(reviewer_role, project));
DROP POLICY IF EXISTS member_respond ON ledger_reviews;
DROP POLICY IF EXISTS reviewer_answers ON ledger_reviews;
CREATE POLICY reviewer_answers ON ledger_reviews FOR UPDATE
    USING (reviewer_role = current_user AND status IN ('requested', 'in_review'))
    WITH CHECK (reviewer_role = current_user
                AND status IN ('in_review', 'approved', 'changes_requested'));
DROP POLICY IF EXISTS requester_cancels ON ledger_reviews;
CREATE POLICY requester_cancels ON ledger_reviews FOR UPDATE
    USING (requester_role = current_user AND status IN ('requested', 'in_review'))
    WITH CHECK (requester_role = current_user AND status = 'cancelled');
DROP TRIGGER IF EXISTS ledger_review_fixed ON ledger_reviews;
CREATE TRIGGER ledger_review_fixed BEFORE UPDATE ON ledger_reviews
    FOR EACH ROW EXECUTE FUNCTION ledger_review_fixed();
DROP TRIGGER IF EXISTS ledger_inbox_ping ON ledger_reviews;
CREATE TRIGGER ledger_inbox_ping AFTER INSERT OR UPDATE ON ledger_reviews
    FOR EACH STATEMENT EXECUTE FUNCTION ledger_inbox_ping();
DROP POLICY IF EXISTS member_read ON ledger_projects;
CREATE POLICY member_read ON ledger_projects FOR SELECT USING (name IN ({_MEMBER}));
DROP POLICY IF EXISTS owner_update ON ledger_projects;
CREATE POLICY owner_update ON ledger_projects FOR UPDATE USING (name IN ({_OWNER}));
DROP POLICY IF EXISTS member_read ON ledger_checkpoints;
CREATE POLICY member_read ON ledger_checkpoints FOR SELECT USING (project IN ({_MEMBER}));
DROP POLICY IF EXISTS member_write ON ledger_checkpoints;
CREATE POLICY member_write ON ledger_checkpoints FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN ({_WRITER}));
DROP POLICY IF EXISTS member_retract ON ledger_checkpoints;
CREATE POLICY member_retract ON ledger_checkpoints FOR UPDATE
    USING (actor_role = current_user OR project IN ({_OWNER}))
    WITH CHECK (actor_role = current_user OR project IN ({_OWNER}));
DROP POLICY IF EXISTS member_read ON ledger_artifacts;
CREATE POLICY member_read ON ledger_artifacts FOR SELECT USING (project IN ({_MEMBER}));
DROP POLICY IF EXISTS member_write ON ledger_artifacts;
CREATE POLICY member_write ON ledger_artifacts FOR INSERT WITH CHECK (
    actor_role = current_user AND project IN ({_WRITER}));
DROP POLICY IF EXISTS member_own ON ledger_access;
DROP POLICY IF EXISTS member_read ON ledger_access;
CREATE POLICY member_read ON ledger_access FOR SELECT
    USING (ledger_is_member(current_user::text, project));
DROP POLICY IF EXISTS member_log ON ledger_audit;
CREATE POLICY member_log ON ledger_audit FOR INSERT WITH CHECK (actor_role = current_user);
DROP POLICY IF EXISTS member_read ON ledger_audit;
CREATE POLICY member_read ON ledger_audit FOR SELECT USING (actor_role = current_user
    OR EXISTS (SELECT 1 FROM ledger_access a WHERE a.member_role = current_user
               AND a.role = 'owner' AND (target = a.project
                   OR left(target, length(a.project) + 1) = a.project || '/')))
"""

# File store on the hub, reached through these functions only. They run as the database
# superuser (who may read and write files under the hub's folder), check the caller's membership
# themselves (session_user is the caller), and accept ids only, never paths.
HUB_FUNCTIONS = """
CREATE OR REPLACE FUNCTION ledger_can(p text, writing boolean) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $f$
  SELECT EXISTS (SELECT 1 FROM ledger_access WHERE member_role = session_user AND project = p
    AND (NOT writing OR role IN ('owner', 'editor')))
$f$;
CREATE OR REPLACE FUNCTION ledger_is_member(r text, p text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $f$
  SELECT EXISTS (SELECT 1 FROM ledger_access WHERE member_role = r AND project = p)
$f$;
CREATE OR REPLACE FUNCTION ledger_owns(p text) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public AS $f$
  SELECT EXISTS (SELECT 1 FROM ledger_access WHERE member_role = session_user AND project = p
    AND role = 'owner')
$f$;
CREATE OR REPLACE FUNCTION ledger_create_project(p text, who text, at text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM ledger_accounts WHERE member_role = session_user) THEN
    RAISE EXCEPTION 'permission denied: no account on this hub';
  END IF;
  IF EXISTS (SELECT 1 FROM ledger_projects WHERE name = p) THEN RETURN false; END IF;
  INSERT INTO ledger_projects(name, created_by, created_at) VALUES (p, who, at);
  INSERT INTO ledger_access VALUES (session_user, p, 'owner',
    (SELECT member FROM ledger_accounts WHERE member_role = session_user));
  RETURN true;
END $f$;
CREATE OR REPLACE FUNCTION ledger_grant(p text, r text, level text) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
DECLARE m text;
BEGIN
  IF NOT ledger_owns(p) THEN
    RAISE EXCEPTION 'permission denied: only the project''s owner adds members';
  END IF;
  IF level NOT IN ('owner', 'editor', 'viewer') THEN RAISE EXCEPTION 'invalid role'; END IF;
  SELECT member INTO m FROM ledger_accounts WHERE member_role = r;
  IF NOT FOUND THEN RAISE EXCEPTION 'NO_ACCOUNT'; END IF;
  INSERT INTO ledger_access VALUES (r, p, level, m)
    ON CONFLICT (member_role, project) DO UPDATE SET role = EXCLUDED.role;
  IF NOT EXISTS (SELECT 1 FROM ledger_access WHERE project = p AND role = 'owner') THEN
    RAISE EXCEPTION 'LAST_OWNER';
  END IF;
  RETURN m;
END $f$;
CREATE OR REPLACE FUNCTION ledger_revoke(p text, r text) RETURNS boolean
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $f$
BEGIN
  IF NOT (ledger_owns(p) OR r = session_user) THEN
    RAISE EXCEPTION 'permission denied: only the project''s owner removes members';
  END IF;
  DELETE FROM ledger_access WHERE member_role = r AND project = p;
  IF NOT FOUND THEN RETURN false; END IF;
  IF NOT EXISTS (SELECT 1 FROM ledger_access WHERE project = p AND role = 'owner') THEN
    RAISE EXCEPTION 'LAST_OWNER';
  END IF;
  RETURN true;
END $f$;
CREATE OR REPLACE FUNCTION ledger_review_fixed() RETURNS trigger
LANGUAGE plpgsql SET search_path = public AS $f$
BEGIN
  IF (NEW.id, NEW.project, NEW.stamp, NEW.requester_role, NEW.reviewer_role, NEW.reviewer,
      NEW.target) IS DISTINCT FROM (OLD.id, OLD.project, OLD.stamp, OLD.requester_role,
      OLD.reviewer_role, OLD.reviewer, OLD.target) THEN
    RAISE EXCEPTION 'permission denied: a review keeps who, what and which version';
  END IF;
  RETURN NEW;
END $f$;
CREATE OR REPLACE FUNCTION ledger_inbox_ping() RETURNS trigger
LANGUAGE plpgsql AS $f$
BEGIN
  PERFORM pg_notify('gm_inbox', '');
  RETURN NULL;
END $f$;
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
  IF a.actor_role <> session_user AND NOT ledger_owns(a.project) THEN
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
HUB_FUNCTION_NAMES = ("ledger_can(text, boolean)", "ledger_is_member(text, text)",
                      "ledger_owns(text)", "ledger_create_project(text, text, text)",
                      "ledger_grant(text, text, text)", "ledger_revoke(text, text)",
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
                 files: Path | None = None, role=""):
        self.db, self.postgres, self.key = connection, postgres, key
        self.where, self.by, self.role = where, by, role
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
            if "NO_ACCOUNT" in text:
                raise ValueError("NO_ACCOUNT: they have no account on this hub yet. The hub's "
                                 "operator adds one (graph-mind-hub add-user <name>) and gives "
                                 "them the code; then add them again") from None
            if "LAST_OWNER" in text:
                raise ValueError("LAST_OWNER: a project keeps at least one owner; make someone "
                                 "else owner first") from None
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
    def create_project(self, name: str, by: str, *, at=None) -> dict:
        """Whoever creates a project owns it: on a hub, an owner row for their own login."""
        name = _project_name(name)
        if self.postgres:
            with self._atomic():
                created = self._sql("SELECT ledger_create_project(?,?,?)",
                                    (name, by, at or _now())).fetchall()[0][0]
                if created:
                    self._audit(by, "create_project", name)
            if not created and name not in self.names():
                raise ValueError(f"PROJECT_NAME_TAKEN: someone on this hub already has a "
                                 f"project called '{name}'; choose another name")
            return {"project": name, "created": created, "owner": by}
        created = not self._sql("SELECT 1 FROM ledger_projects WHERE name=?", (name,)).fetchall()
        if created:
            with self._atomic():
                self._sql("INSERT INTO ledger_projects(name, created_by, created_at) "
                          "VALUES(?,?,?)", (name, by, at or _now()))
                self._audit(by, "create_project", name)
        return {"project": name, "created": created, "owner": by}

    # -- members (a project's owner manages them) -------------------------------------------
    def grant(self, project, member, role, by) -> dict:
        """Add a member who has an account on this hub to `project`, or change their role.
        Owner only, checked on the hub."""
        if role not in ROLES:
            raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
        project = self._require(project)
        self._needs_hub()
        with self._atomic():
            name = self._sql("SELECT ledger_grant(?,?,?)",
                             (project, _member_role(member), role)).fetchall()[0][0]
            self._audit(by, "invite", f"{project}/{name} {role}")
        return {"project": project, "member": name, "role": role}

    def revoke(self, project, member, by) -> dict:
        """Take `project` away from a member (owner only), or leave it yourself."""
        project = self._require(project)
        self._needs_hub()
        with self._atomic():
            removed = self._sql("SELECT ledger_revoke(?,?)",
                                (project, _member_role(member))).fetchall()[0][0]
            self._audit(by, "remove_member", f"{project}/{member}")
        return {"project": project, "member": member, "removed": bool(removed),
                "note": "Their access to this project ended now. Files they already "
                        "downloaded stay on their PC."}

    def _needs_hub(self):
        if not self.postgres:
            raise ValueError("NO_HUB: this project is on this PC only; host a hub (or move it "
                             "to one) before adding people")

    def members(self, project) -> list[dict]:
        project = self._require(project)
        if not self.postgres:
            by = self._sql("SELECT created_by FROM ledger_projects WHERE name=?",
                           (project,)).fetchall()[0][0]
            return [{"member": by, "role": "owner"}]
        rows = self._sql("SELECT member, role FROM ledger_access WHERE project=? "
                         "ORDER BY role, member", (project,)).fetchall()
        return [{"member": member, "role": role} for member, role in rows]

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
                supersedes=None, details=None, context=None) -> dict:
        """`summary`: one line. `details`: what changed and why, files and functions, test
        results, links, what is left; a handover note for a teammate, never pasted chat."""
        project = self._require(project)
        checkpoint = self._checkpoint_record(project, kind, summary, by, artifacts, source_refs,
                                             supersedes, details, context)
        with self._atomic():
            self._insert_checkpoint(checkpoint)
        return {**checkpoint, "status": "published"}

    def _checkpoint_record(self, project, kind, summary, by, artifacts, source_refs, supersedes,
                           details, context=None) -> dict:
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
                "artifacts": artifacts or [], "source_refs": refs, "supersedes": supersedes,
                "context": _clean_context(context)}

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
            self._audit(by, "retract", f"{checkpoint['project']}/{checkpoint_id}")
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
               details=None, context=None) -> dict:
        """Publish what the user made: a file (a new version, never an overwrite), a demo
        folder (index.html and what it loads, as one bundle), or for code a link to the git
        commit at the stage it reached."""
        project = self._require(project)
        path = Path(path).expanduser().resolve()
        if path.is_dir():
            if (path / "index.html").is_file():
                return self._publish_file(project, path.name + ".zip", _bundle(path), summary,
                                          by, kind, source_refs, details, bundle=True,
                                          context=context)
            link = git_link(path)
            if link is None:
                raise ValueError("FOLDER_NOT_UPLOADED: a folder is shared as a demo bundle "
                                 "(it needs an index.html) or, for code, as a git link; "
                                 "otherwise attach its files")
            summary = summary or f"{link['repo']} {link['stage']} @ {link['commit'][:8]}"
            return self.publish(project, kind, summary, by, artifacts=[link],
                                source_refs=source_refs, details=details, context=context)
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
                                  details, extra=extra, context=context)

    def _publish_file(self, project, name, data, summary, by, kind, source_refs, details, *,
                      bundle=False, extra=None, context=None) -> dict:
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
                                                 by, [artifact], source_refs, None, details,
                                                 context)
            self._sql("INSERT INTO ledger_artifacts(artifact_id, project, version, parts, size, "
                      "location, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                      (artifact_id, project, version, parts, len(data), location, "published",
                       _now()))
            self._insert_checkpoint(checkpoint)
        return {**checkpoint, "status": "published"}

    def fetch(self, project, *, name=None, version=None, into: Path, by=None,
              artifact_id=None) -> dict:
        """The latest (or given) version of a result, written under `into` and hash-checked; a
        demo bundle is unpacked and its index.html returned."""
        project = self._require(project)
        wanted = (name or "").casefold()
        for checkpoint in self.timeline(project, limit=10**6):
            for artifact in checkpoint.get("artifacts", []):
                if not artifact.get("artifact_id"):
                    continue
                if artifact_id and artifact["artifact_id"] != artifact_id:
                    continue
                if wanted and wanted not in artifact["name"].casefold():
                    continue
                if version and artifact["version"] != int(version):
                    continue
                data = self._bytes(artifact)
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

    def _bytes(self, artifact) -> bytes:
        data = b"".join(self.store.get(artifact["location"], artifact["parts"]))
        if hashlib.sha256(data).hexdigest() != artifact["sha256"]:
            raise ValueError("ARTIFACT_DAMAGED: the stored file does not match its hash")
        return data

    def _target(self, project, *, checkpoint_id=None, name=None, version=None):
        """(checkpoint, result) a review or comparison is about: newest match first."""
        wanted = (name or "").casefold()
        for checkpoint in self.timeline(project, limit=10**6):
            if checkpoint_id and checkpoint["id"] != checkpoint_id:
                continue
            for artifact in checkpoint.get("artifacts", []):
                label = artifact.get("name") or artifact.get("repo") or ""
                if wanted and wanted not in label.casefold():
                    continue
                if version and artifact.get("version") != int(version):
                    continue
                return checkpoint, artifact
        raise ValueError("RESULT_NOT_FOUND: no published result matches"
                         + (f" '{name}'" if name else "") + (f" v{version}" if version else ""))

    # -- reviews ----------------------------------------------------------------------------
    def _reviewer_role(self, project, reviewer) -> str:
        if not self.postgres:
            return ""
        known = self._sql("SELECT member_role FROM ledger_access WHERE project=? AND member=?",
                          (project, reviewer)).fetchall()
        return known[0][0] if known else _member_role(reviewer)

    def request_review(self, project, reviewer, by, *, checkpoint_id=None, name=None,
                       version=None, focus=None, note=None) -> dict:
        """Ask a project member to review one exact version (or commit). The same open request
        is never made twice, so nobody is notified twice."""
        project = self._require(project)
        reviewer = str(reviewer or "").strip()
        if not reviewer:
            raise ValueError("REVIEWER_REQUIRED: who should review it")
        checkpoint, artifact = self._target(project, checkpoint_id=checkpoint_id, name=name,
                                            version=version)
        target = (f"{artifact['artifact_id']}@v{artifact['version']}" if artifact.get("artifact_id")
                  else f"git:{artifact.get('commit')}")
        for existing in self.reviews(project):
            if (existing["target"] == target and existing["reviewer"] == reviewer
                    and existing["status"] in OPEN_REVIEW):
                return {**existing, "duplicate": True,
                        "note": "This exact version is already waiting for this reviewer; no "
                                "second request or notification was made."}
        focus = str(focus or "").strip()[:2000] or (checkpoint.get("context") or {}).get(
            "review_focus", "")
        note = str(note or "").strip()[:2000]
        found = scan_text(focus + "\n" + note)
        if found:
            raise ValueError("SECRET_IN_REVIEW: " + ", ".join(found))
        review = {"id": "rv-" + uuid.uuid4().hex[:12], "project": project, "requester": by,
                  "reviewer": reviewer, "target": target, "checkpoint_id": checkpoint["id"],
                  "artifact": {k: artifact[k] for k in REVIEW_TARGET_FIELDS if k in artifact},
                  "focus": focus or NOT_RECORDED, "note": note, "requested_at": _now(),
                  "opened_at": None, "responded_at": None, "response": None}
        try:
            with self._atomic():
                self._sql("INSERT INTO ledger_reviews(id, project, stamp, status, reviewer_role, "
                          "reviewer, target, payload) VALUES(?,?,?,?,?,?,?,?)",
                          (review["id"], project, _stamp(), "requested",
                           self._reviewer_role(project, reviewer), reviewer, target,
                           self._store(review)))
                self._audit(by, "request_review", f"{project}/{review['id']} -> {reviewer}")
        except ValueError as error:
            if "NOT_ALLOWED" in str(error):
                raise ValueError(f"REVIEWER_NOT_IN_PROJECT: {reviewer} has no access to "
                                 f"'{project}' (or your role cannot request reviews)") from None
            raise
        return {**review, "status": "requested"}

    def reviews(self, project=None, *, mine=False) -> list[dict]:
        """Review requests, newest first. `mine`: the ones waiting for this person and the
        answers to the ones they asked for."""
        query = ("SELECT status, reviewer_role, requester_role, payload FROM ledger_reviews"
                 + (" WHERE project=?" if project else "") + " ORDER BY stamp DESC")
        out = []
        for status, reviewer_role, requester_role, payload in self._sql(
                query, (project,) if project else ()).fetchall():
            review = {**self._payload(payload), "status": status}
            if mine:
                to_me = (reviewer_role == self.role) if self.postgres else (
                    review["reviewer"] == self.by)
                from_me = (requester_role == self.role) if self.postgres else (
                    review["requester"] == self.by)
                if not (to_me or from_me):
                    continue
                review["for_me"] = "to review" if to_me else "my request"
            out.append(review)
        return out

    def _review(self, review_id):
        rows = self._sql("SELECT status, reviewer_role, requester_role, payload FROM "
                         "ledger_reviews WHERE id=?", (review_id,)).fetchall()
        if not rows:
            raise ValueError(f"REVIEW_NOT_FOUND: {review_id}")
        status, reviewer_role, requester_role, payload = rows[0]
        return self._payload(payload), status, reviewer_role, requester_role

    def _is_reviewer(self, review, reviewer_role) -> bool:
        return reviewer_role == self.role if self.postgres else review["reviewer"] == self.by

    def _save_review(self, review_id, review, status, by, action):
        with self._atomic():
            updated = self._sql("UPDATE ledger_reviews SET status=?, payload=? WHERE id=?",
                                (status, self._store(review), review_id))
            if getattr(updated, "rowcount", 1) == 0:
                raise ValueError("NOT_ALLOWED: only the reviewer (or the requester, to cancel) "
                                 "can change this review")
            self._audit(by, action, f"{review['project']}/{review_id}")

    def open_review(self, review_id, *, into, by) -> dict:
        """Everything a reviewer's AI needs: the exact version, what changed since the last
        one (measured from the files), the publisher's account of it (marked as such, with
        "not recorded" where nothing was written), the decisions and earlier reviews it builds
        on, and what to check. Opening is not approving: the status only becomes in_review."""
        review, status, reviewer_role, _ = self._review(review_id)
        project = review["project"]
        checkpoint, _ = self._checkpoint(review["checkpoint_id"])
        artifact = review["artifact"]
        result = {"review": {**review, "status": status},
                  "checkpoint": {"id": checkpoint["id"], "summary": checkpoint["summary"],
                                 "by": checkpoint["actor"], "at": checkpoint["created_at"],
                                 "details": checkpoint.get("details") or NOT_RECORDED},
                  "context": context_view(checkpoint),
                  "history": self._history(project, checkpoint, artifact)}
        if artifact.get("artifact_id"):
            result["file"] = self.fetch(project, artifact_id=artifact["artifact_id"],
                                        into=into, by=by)
            result["changes"] = self.compare(project, artifact["name"],
                                             version=artifact["version"])
        else:
            result["code"] = artifact
            result["changes"] = {"note": "Code is reviewed on GitHub: the commit and PR above "
                                         "carry the diff and the checks."}
        if status == "requested" and self._is_reviewer(review, reviewer_role):
            review["opened_at"] = _now()
            self._save_review(review_id, review, "in_review", by, "open_review")
            result["review"].update(status="in_review", opened_at=review["opened_at"])
        return result

    def respond_review(self, review_id, decision, by, *, note=None, version=None) -> dict:
        """Record the reviewer's own decision on the exact version asked about. Only on the
        person's explicit word ("승인해", "수정 요청해"): never because a file was opened."""
        if decision not in ("approved", "changes_requested"):
            raise ValueError("DECISION must be approved or changes_requested")
        review, status, reviewer_role, _ = self._review(review_id)
        if not self._is_reviewer(review, reviewer_role):
            raise ValueError("NOT_ALLOWED: only the requested reviewer can answer it")
        if status not in OPEN_REVIEW:
            raise ValueError(f"REVIEW_CLOSED: it is already {status}")
        asked = review["artifact"].get("version")
        if version is not None and asked is not None and int(version) != asked:
            raise ValueError(f"WRONG_VERSION: this review is for v{asked}; v{version} needs its "
                             "own review request")
        text = str(note or "").strip()[:4000]
        found = scan_text(text)
        if found:
            raise ValueError("SECRET_IN_REVIEW: " + ", ".join(found))
        review["responded_at"] = _now()
        review["response"] = {"decision": decision, "note": text, "by": by,
                              "target": review["target"], "version": asked}
        self._save_review(review_id, review, decision, by, decision)
        return {**review, "status": decision,
                "applies_to": f"{review['target']} only: a later version needs its own review"}

    def cancel_review(self, review_id, by) -> dict:
        review, status, _, requester_role = self._review(review_id)
        mine = (requester_role == self.role) if self.postgres else review["requester"] == by
        if not mine:
            raise ValueError("NOT_ALLOWED: only who asked for the review can cancel it")
        if status not in OPEN_REVIEW:
            raise ValueError(f"REVIEW_CLOSED: it is already {status}")
        self._save_review(review_id, review, "cancelled", by, "cancel_review")
        return {**review, "status": "cancelled"}

    def compare(self, project, name, *, version=None, against=None) -> dict:
        """What changed between two versions of a result, measured from the stored files."""
        project = self._require(project)
        _, new = self._target(project, name=name, version=version)
        if not new.get("artifact_id"):
            return {"code": new, "note": "Code: compare commits on GitHub."}
        earlier = [a for c in self.timeline(project, limit=10**6) for a in c.get("artifacts", [])
                   if a.get("name") == new["name"] and a.get("artifact_id")
                   and a["version"] < new["version"]
                   and (against is None or a["version"] == int(against))]
        if not earlier:
            return {"name": new["name"], "version": new["version"], "previous": None,
                    "changes": "first version: nothing earlier to compare with"}
        old = earlier[0]
        return {"name": new["name"], "from": old["version"], "to": new["version"],
                "changes": describe_changes(new["name"], self._bytes(old), self._bytes(new)),
                "source": "measured from the two stored files, not from anyone's summary"}

    def _history(self, project, checkpoint, artifact) -> dict:
        """The decisions, feedback and earlier versions this result builds on: what the
        checkpoint says it addresses, plus the earlier versions and reviews of the same result."""
        label = artifact.get("name") or artifact.get("repo")
        addressed = set((checkpoint.get("context") or {}).get("addresses") or [])
        brief = lambda c: {"id": c["id"], "type": c["type"], "summary": c["summary"],  # noqa: E731
                           "by": c["actor"], "at": c["created_at"], "status": c["status"],
                           "context": c.get("context") or {}}
        linked, earlier = [], []
        for c in self.timeline(project, limit=10**6):
            if c["id"] == checkpoint["id"]:
                continue
            if c["id"] in addressed:
                linked.append(brief(c))
            elif any((a.get("name") or a.get("repo")) == label for a in c.get("artifacts", [])):
                earlier.append(brief(c))
        reviews = [{"id": r["id"], "target": r["target"], "status": r["status"],
                    "reviewer": r["reviewer"], "focus": r.get("focus"),
                    "response": r.get("response")}
                   for r in self.reviews(project)
                   if r["id"] in addressed
                   or (r["artifact"].get("name") or r["artifact"].get("repo")) == label]
        return {"addresses": linked + [r for r in reviews if r["id"] in addressed]
                or NOT_RECORDED,
                "earlier_versions": earlier[:5], "reviews_of_this_result": reviews[:10]}


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


# -- work context, reviews and notifications ---------------------------------------------------
NOT_RECORDED = "not recorded"
OPEN_REVIEW = ("requested", "in_review")
REVIEW_TARGET_FIELDS = ("kind", "artifact_id", "name", "version", "sha256", "size", "repo",
                        "branch", "commit", "stage", "pr", "release")
# what a checkpoint says about its work. The text fields are the publishing AI's own account;
# `addresses` (ids of the decisions, feedback and reviews it answers) and `evidence` (commit,
# PR, test log or document ids and links) point at the record itself.
CONTEXT_TEXT = ("what", "where", "why", "verification", "remaining", "review_focus")
CONTEXT_LINKS = ("addresses", "evidence")


def _clean_context(context) -> dict:
    if not context:
        return {}
    if not isinstance(context, dict):
        raise ValueError("CONTEXT: an object with " + ", ".join(CONTEXT_TEXT + CONTEXT_LINKS))
    out = {}
    for key, value in context.items():
        if key in CONTEXT_LINKS:
            items = [str(v).strip() for v in (value if isinstance(value, list) else [value])
                     if str(v).strip()]
            if any(len(item) > 300 for item in items):
                raise ValueError(f"CONTEXT_{key.upper()}: ids and links only, not text")
            out[key] = items
        elif key in CONTEXT_TEXT:
            text = str(value or "").strip()
            if len(text) > 2000:
                raise ValueError(f"CONTEXT_{key.upper()}_TOO_LONG: 2,000 characters at most")
            if text:
                out[key] = text
        else:
            raise ValueError(f"CONTEXT_FIELD '{key}': use " + ", ".join(CONTEXT_TEXT + CONTEXT_LINKS))
    found = scan_text(json.dumps(out, ensure_ascii=False))
    if found:
        raise ValueError("SECRET_IN_CONTEXT: " + ", ".join(found))
    return out


def context_view(checkpoint) -> dict:
    """The publisher's account and the links to the record, kept apart; a missing part says
    "not recorded" so an AI never fills it in."""
    context = checkpoint.get("context") or {}
    code = [a for a in checkpoint.get("artifacts", []) if a.get("kind") == "git"]
    return {"publishers_account": {key: context.get(key) or NOT_RECORDED for key in CONTEXT_TEXT},
            "record": {"addresses": context.get("addresses") or NOT_RECORDED,
                       "evidence": context.get("evidence") or NOT_RECORDED,
                       "source_refs": checkpoint.get("source_refs") or NOT_RECORDED,
                       "code": code or NOT_RECORDED}}


def _xml_text(fragment: str, tag: str) -> str:
    import html
    return html.unescape("".join(re.findall(rf"<{tag}[^>]*>(.*?)</{tag}>", fragment, re.S)))


def readable_lines(name: str, data: bytes) -> list[str] | None:
    """The text of a result, line by line, labelled by slide, paragraph, sheet cell or file, so
    two versions can be compared; None for binaries (images, PDFs, installers)."""
    lower = name.lower()
    try:
        if lower.endswith((".pptx", ".docx", ".xlsx", ".zip")) and zipfile.is_zipfile(io.BytesIO(data)):
            archive = zipfile.ZipFile(io.BytesIO(data))
            names = archive.namelist()
            number = lambda n: int(re.findall(r"(\d+)\.xml$", n)[0])  # noqa: E731
            if lower.endswith(".pptx"):
                slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                                key=number)
                return [f"[slide {number(n)}] {_xml_text(p, 'a:t')}" for n in slides
                        for p in re.findall(r"<a:p[ >].*?</a:p>", archive.read(n).decode("utf-8",
                                                                                         "replace"), re.S)
                        if _xml_text(p, "a:t").strip()]
            if lower.endswith(".docx"):
                body = archive.read("word/document.xml").decode("utf-8", "replace")
                return [_xml_text(p, "w:t") for p in re.findall(r"<w:p[ >].*?</w:p>", body, re.S)
                        if _xml_text(p, "w:t").strip()]
            if lower.endswith(".xlsx"):
                shared = []
                if "xl/sharedStrings.xml" in names:
                    shared = [_xml_text(si, "t") for si in re.findall(
                        r"<si>.*?</si>", archive.read("xl/sharedStrings.xml").decode("utf-8", "replace"), re.S)]
                lines = []
                for sheet in sorted((n for n in names if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                                    key=number):
                    xml = archive.read(sheet).decode("utf-8", "replace")
                    for ref, attrs, inner in re.findall(r'<c r="([A-Z]+\d+)"([^>]*)>(.*?)</c>', xml, re.S):
                        value = re.findall(r"<v>(.*?)</v>", inner)
                        if not value:
                            continue
                        text = shared[int(value[0])] if 't="s"' in attrs else value[0]
                        lines.append(f"[sheet {number(sheet)} {ref}] {text}")
                return lines
            lines = []                                    # a demo bundle: file by file
            for member in sorted(names):
                content = archive.read(member)
                if b"\0" in content[:8192] or len(content) > 2 * 2**20:
                    lines.append(f"[{member}] binary, {len(content)} bytes, "
                                 f"sha256 {hashlib.sha256(content).hexdigest()[:12]}")
                else:
                    lines += [f"[{member}] {line}" for line in
                              content.decode("utf-8", "replace").splitlines()]
            return lines
    except (zipfile.BadZipFile, KeyError, IndexError, ValueError):
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None


def describe_changes(name: str, old: bytes, new: bytes) -> dict:
    import difflib
    if old == new:
        return {"identical": True}
    before, after = readable_lines(name, old), readable_lines(name, new)
    if before is None or after is None:
        return {"binary": True, "size": f"{len(old)} -> {len(new)} bytes",
                "note": "The content differs; it has no text to compare line by line."}
    diff = [line for line in difflib.unified_diff(before, after, "before", "after", n=1,
                                                  lineterm="")][2:]
    added = sum(line.startswith("+") for line in diff)
    removed = sum(line.startswith("-") for line in diff)
    shown, size = [], 0
    for line in diff:
        if len(shown) >= 300 or size > 8000:
            shown.append(f"... {len(diff) - len(shown)} more lines")
            break
        shown.append(line)
        size += len(line)
    return {"lines_added": added, "lines_removed": removed, "diff": shown}


def inbox_events(ledger) -> list[dict]:
    """What this person should hear about on one ledger: reviews waiting for them, and answers
    to the reviews they asked for. Only that project's shared content goes in a notification."""
    events = []
    for review in ledger.reviews(mine=True):
        artifact = review["artifact"]
        what = artifact.get("name") or artifact.get("repo") or "result"
        if artifact.get("version"):
            what += f" v{artifact['version']}"
        if review["for_me"] == "to review" and review["status"] == "requested":
            events.append({"key": f"{review['id']}:requested", "review_id": review["id"],
                           "project": review["project"], "title": f"[{review['project']}] 검토 요청",
                           "body": f"{review['requester']}님이 {what} 검토를 요청했습니다."})
        elif review["for_me"] == "my request" and review["status"] in ("approved",
                                                                         "changes_requested"):
            word = "승인" if review["status"] == "approved" else "수정 요청"
            note = (review.get("response") or {}).get("note") or ""
            events.append({"key": f"{review['id']}:{review['status']}", "review_id": review["id"],
                           "project": review["project"], "title": f"[{review['project']}] {word}",
                           "body": f"{review['reviewer']}님이 {what}을(를) {word}했습니다."
                                   + (f" {note[:80]}" if note else "")})
    return events


def notify_new(show=True) -> list[dict]:
    """Deliver what is new since last time, once each: a PC notification per event. Only one
    that was shown is recorded as delivered: a notification that failed, or a hub offline now,
    is tried again on the next round, so nothing is lost."""
    state = _home() / "notified.json"
    try:
        seen = set(json.loads(state.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        seen = set()
    found, _ = ledgers()
    fresh = []
    for ledger in found:
        try:
            fresh += [event for event in inbox_events(ledger) if event["key"] not in seen]
        except Exception:
            pass
        finally:
            ledger.db.close()
    delivered = [event for event in fresh if not show or toast(event["title"], event["body"])]
    seen.update(event["key"] for event in delivered)
    if delivered:
        state.parent.mkdir(parents=True, exist_ok=True)
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps(sorted(seen)), encoding="utf-8")
        os.replace(temporary, state)
    return delivered


def watch_inbox(stop, fallback: float = 120.0) -> None:
    """Notifications in real time. One listening connection per hub: the hub signals the moment
    a review is requested or answered (an empty NOTIFY: no content, so nobody learns anything
    from it), and notify_new then reads what is new through the usual permission checks. It
    reads every `fallback` seconds anyway, and a dropped hub is listened to again (catching up
    first), so nothing waits on a signal that never came. Runs until `stop` is set."""
    import threading
    wake, listening = threading.Event(), {}

    def listen(url):
        import psycopg
        while not stop.is_set():
            try:
                with psycopg.connect(url, autocommit=True, connect_timeout=5) as connection:
                    connection.execute("LISTEN gm_inbox")
                    wake.set()                       # (re)connected: catch up on what was missed
                    while not stop.is_set():
                        for _ in connection.notifies(timeout=30, stop_after=1):
                            wake.set()
                        connection.execute("SELECT 1")   # a dropped connection raises here
            except Exception:
                stop.wait(30)

    while not stop.is_set():
        for key, url in _hub_urls().items():
            if key not in listening or not listening[key].is_alive():
                listening[key] = threading.Thread(target=listen, args=(url,), daemon=True,
                                                  name="graph-mind-inbox")
                listening[key].start()
        try:
            notify_new()
        except Exception:
            pass
        wake.wait(fallback)
        wake.clear()


def _hub_urls() -> dict:
    """Every hub holding this person's project brains: their own, and each team hub."""
    import brain_log
    settings, urls = brain_log._config(), {}
    try:
        if settings.get("hosts_projects"):
            urls["own"] = brain_log.local_postgres()
        elif brain_log.is_database(brain_log.folder()):
            urls["own"] = brain_log.folder()
    except Exception:
        pass                                     # down now: the fallback round finds it later
    for code in settings.get("team_hubs", []):
        try:
            urls[code] = _team_url(_decode_team(code))
        except Exception:
            pass
    return urls


def toast(title: str, body: str) -> bool:
    """A desktop notification, and whether it was shown. Text goes through the environment,
    never into a command line."""
    environment = {**os.environ, "GM_TITLE": title, "GM_BODY": body}
    quiet = {"capture_output": True, "timeout": 20, "stdin": subprocess.DEVNULL, "env": environment}
    try:
        if os.name == "nt":
            script = (
                "$ErrorActionPreference = 'Stop';"          # any failure: exit code 1
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, "
                "ContentType = WindowsRuntime] > $null;"
                "$x = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$t = $x.GetElementsByTagName('text');"
                "$t.Item(0).AppendChild($x.CreateTextNode($env:GM_TITLE)) > $null;"
                "$t.Item(1).AppendChild($x.CreateTextNode($env:GM_BODY)) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("
                "'{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe')"
                ".Show([Windows.UI.Notifications.ToastNotification]::new($x))")
            shown = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                                    script], creationflags=0x08000000, **quiet)
        elif sys.platform == "darwin":
            shown = subprocess.run(["osascript", "-e", "on run argv\ndisplay notification (item "
                                    "2 of argv) with title (item 1 of argv)\nend run", title,
                                    body], **quiet)
        elif shutil.which("notify-send"):
            shown = subprocess.run(["notify-send", title, body], **quiet)
        else:
            return False                         # nothing to show it with: asked again later
        return shown.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


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
                "k": str(value["k"]), "l": str(value["l"]), "n": str(value["n"]),
                "c": value.get("c")}
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
        Ledger(owner, postgres=True)                     # the hosting role owns the tables
    with _admin() as admin:                              # what was written before sharing
        Ledger(admin, postgres=True, create=False,       # gets sealed now, every project's
               key=_fernet(ledger_secret(password)) if password else None).seal_existing()
        # this hub's accounts: the hosting person's own role, and every teammate invited so far.
        # Once, on a hub from before owners existed: its projects belong to the hosting person,
        # the only one who could create them then.
        first = not admin.execute("SELECT 1 FROM ledger_accounts LIMIT 1").fetchall()
        admin.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON CONFLICT DO NOTHING",
                      (OWNER_ROLE, actor()))
        if first:
            admin.execute("INSERT INTO ledger_accounts SELECT DISTINCT ON (member_role) "
                          "member_role, member FROM ledger_access ON CONFLICT DO NOTHING")
            admin.execute("INSERT INTO ledger_access SELECT %s, name, 'owner', created_by FROM "
                          "ledger_projects ON CONFLICT (member_role, project) DO UPDATE SET "
                          "role = 'owner'", (OWNER_ROLE,))
    root = hub_artifacts().as_posix().replace("'", "''")
    with _admin() as admin:            # the functions before the policies that call them
        for statement in HUB_FUNCTIONS.format(root=root).split("$f$;"):
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
        # what a role may do in each project is the policies' business: the editor/viewer and
        # owner checks are per project there
        admin.execute(f"GRANT SELECT, INSERT ON ledger_audit TO {TEAM_GROUP}")
        admin.execute(f"GRANT SELECT, INSERT, UPDATE ON ledger_reviews TO {TEAM_GROUP}")
        admin.execute(f"GRANT INSERT, UPDATE ON ledger_checkpoints TO {TEAM_GROUP}")
        admin.execute(f"GRANT INSERT ON ledger_artifacts TO {TEAM_GROUP}")
        admin.execute(f"GRANT UPDATE (instructions) ON ledger_projects TO {TEAM_GROUP}")
        # a hub that hosts only project brains has no personal memory table at all
        if admin.execute("SELECT to_regclass('public.brain_log')").fetchone()[0]:
            admin.execute(f"REVOKE ALL ON brain_log FROM {TEAM_GROUP}")
    with psycopg.connect(owner_url, autocommit=True) as owner:
        for statement in POLICIES.split(";"):
            if statement.strip():
                owner.execute(statement)


def secure_hub() -> None:
    """prepare_hub, and let teammates (group gm_team) log in from the network."""
    import brain_log
    prepare_hub()
    brain_log.allow_from_network(f"graphmind +{TEAM_GROUP}", "teammates, project brains only")


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
                hub.create_project(name, by, at=at)
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


def add_account(member: str, by: str = "hub operator", *, reset: bool = False) -> dict:
    """On the hub's machine: an account for a person, with no project at all, and the code to
    give them. Owners then add the account to their projects; the operator never does. An
    existing account keeps its code unless `reset` (a lost code): a new code ends the old one."""
    import brain_log
    import secrets
    member = str(member or "").strip()
    if not member or len(member) > 60:
        raise ValueError("MEMBER_NAME: the person's name, as others will see it")
    opened = brain_log.open_to_network()
    secure_hub()
    login = _member_role(member)
    with _admin() as admin:
        exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall()
        if exists and not reset:
            return {"member": member, "account": "exists", "team_code": None,
                    "note": "They already have an account and their code still works. A "
                            "project's owner adding them is enough. Lost code: reset it."}
        password = secrets.token_urlsafe(18)
        if exists:
            admin.execute(f"ALTER ROLE {login} LOGIN PASSWORD '{password}'")
        else:
            admin.execute(f"CREATE ROLE {login} LOGIN PASSWORD '{password}' IN ROLE {TEAM_GROUP}")
        admin.execute("INSERT INTO ledger_accounts VALUES(%s,%s) ON CONFLICT (member_role) DO "
                      "UPDATE SET member = EXCLUDED.member", (login, member))
        admin.execute("INSERT INTO ledger_audit(stamp, at, actor_role, actor, action, target) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), "postgres", by,
                                                     "reset_account" if exists else "add_account",
                                                     f"account {member}"))
    secret = ledger_secret(brain_log._config()["share_password"])
    code = TEAM_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": opened["addresses"], "p": brain_log.PORT, "u": login, "k": password,
         "l": base64.urlsafe_b64encode(secret).decode(), "n": member,
         "c": opened["certificate"]}).encode()).decode().rstrip("=")
    return {"member": member, "account": "reset" if exists else "created", "team_code": code,
            "firewall": opened["firewall"],
            "note": "Give this code to them privately (it works like a password). On their PC "
                    "they paste it to their AI, which joins the hub. They see no project until "
                    "its owner adds them, and can create their own. Never the gm1. code: that "
                    "is your personal memory."}


def accounts() -> list[dict]:
    """The operator's view: who has an account and their role per project. Names only; the
    operator's tools never read what is in a project."""
    with _admin() as admin:
        people = admin.execute("SELECT member_role, member FROM ledger_accounts "
                               "ORDER BY member").fetchall()
        access = admin.execute("SELECT member_role, project, role FROM ledger_access").fetchall()
    out = []
    for login, member in people:
        out.append({"member": member, "hosting_person": login == OWNER_ROLE,
                    "projects": {p: r for who, p, r in access if who == login}})
    return out


def remove_account(member: str, by: str = "hub operator", *, transfer_to: str = None) -> dict:
    """Close a person's account on this hub: their login ends now. A project they alone own
    needs a new owner first (`transfer_to`, someone with an account)."""
    login = _member_role(member)
    with _admin() as admin:
        sole = [p for (p,) in admin.execute(
            "SELECT project FROM ledger_access a WHERE member_role=%s AND role='owner' AND NOT "
            "EXISTS (SELECT 1 FROM ledger_access b WHERE b.project=a.project AND b.role='owner' "
            "AND b.member_role<>a.member_role) ORDER BY project", (login,)).fetchall()]
        if sole and not transfer_to:
            raise ValueError("LAST_OWNER: they alone own " + ", ".join(sole) + "; name who takes "
                             "them over (transfer_to), or have them make someone owner first")
        if sole:
            heir = _member_role(transfer_to)
            found = admin.execute("SELECT member FROM ledger_accounts WHERE member_role=%s",
                                  (heir,)).fetchall()
            if not found:
                raise ValueError(f"NO_ACCOUNT: {transfer_to} has no account on this hub")
            for project in sole:
                admin.execute("INSERT INTO ledger_access VALUES(%s,%s,'owner',%s) ON CONFLICT "
                              "(member_role, project) DO UPDATE SET role='owner'",
                              (heir, project, found[0][0]))
        admin.execute("DELETE FROM ledger_access WHERE member_role=%s", (login,))
        admin.execute("DELETE FROM ledger_accounts WHERE member_role=%s", (login,))
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (login,)).fetchall():
            admin.execute(f"DROP OWNED BY {login}")
            admin.execute(f"DROP ROLE {login}")
        admin.execute("INSERT INTO ledger_audit(stamp, at, actor_role, actor, action, target) "
                      "VALUES(%s,%s,%s,%s,%s,%s)", (_stamp(), _now(), "postgres", by,
                                                     "remove_account", f"account {member}"))
    return {"member": member, "removed": True, "transferred": {p: transfer_to for p in sole},
            "note": "Their access ended now. Files they already downloaded stay on their PC."}


def hub_audit(limit=100) -> list[dict]:
    with _admin() as admin:
        rows = admin.execute("SELECT at, actor, action, target FROM ledger_audit ORDER BY stamp "
                             "DESC LIMIT %s", (limit,)).fetchall()
    return [{"at": at, "actor": who, "action": action, "target": target}
            for at, who, action, target in rows]


def invite(member: str, role: str, projects: list[str], by: str) -> dict:
    """The owner, on the PC hosting their hub: an account for the teammate if they have none
    (with the code to give them), and access to `projects`. Projects kept on this PC alone
    move into the hub first."""
    if role not in ROLES:
        raise ValueError(f"ROLE must be one of {', '.join(ROLES)}")
    if not projects:
        raise ValueError("PROJECTS_REQUIRED: which projects this teammate may see")
    if not _hosts_here():
        host_projects()
    result = add_account(member, by)
    ledger = own_ledger()
    try:
        granted = [ledger.grant(p, member, role, by)["project"] for p in projects]
    finally:
        ledger.db.close()
    return {**result, "role": role, "projects": granted}


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
    import brain_log
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
            f"@{host}:{value['p']}/graphmind" + brain_log.tls_options(value["c"]))


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
                  create=False, where=f"team hub at {host} (as {value['n']})", by=value["n"],
                  role=value["u"])


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
        return Ledger(connection, postgres=True, key=key, where="your project hub", by=actor(),
                      role=OWNER_ROLE)
    path = _home() / "ledger.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Ledger(sqlite3.connect(path), by=actor(),
                  where="this PC only (no hub yet: host or join one so others see it)")


def home_ledger(hub: str | None = None) -> Ledger:
    """Where a new project goes: the person's own hub, or this PC alone, unless they work only
    on team hubs (a company server): then the team hub, or the one `hub` names."""
    import brain_log
    settings = brain_log._config()
    codes = settings.get("team_hubs", [])
    own = settings.get("hosts_projects") or brain_log.is_database(brain_log.folder())
    if hub or (codes and not own):
        label = lambda c: ", ".join(_decode_team(c)["h"])  # noqa: E731
        matches = [c for c in codes if not hub or hub in label(c)]
        if len(matches) == 1:
            return _team_ledger(matches[0])
        raise ValueError("HUB_REQUIRED: say which hub the project goes on (`hub`): "
                         + "; ".join(label(c) for c in codes))
    return own_ledger()


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


def ledger_with_review(review_id: str) -> Ledger:
    """The ledger holding this review request (it may be on any hub this person is in)."""
    found, unreachable = ledgers()
    chosen = None
    for ledger in found:
        hit = chosen is None and ledger._sql("SELECT 1 FROM ledger_reviews WHERE id=?",
                                             (review_id,)).fetchall()
        if hit:
            chosen = ledger
        else:
            ledger.db.close()
    if chosen is None:
        if unreachable:
            raise HubUnavailable(", ".join(unreachable))
        raise ValueError(f"REVIEW_NOT_FOUND: {review_id}")
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


def hub_main(argv=None) -> int:
    """graph-mind-hub: run a hub on a server or NAS with no AI on it. The operator makes it a
    hub and manages accounts; owners manage their projects from their own AI."""
    import argparse
    parser = argparse.ArgumentParser(prog="graph-mind-hub", description=hub_main.__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("host", help="make this machine a hub (once; it stays one)")
    add = commands.add_parser("add-user", help="an account for a person, and their code")
    add.add_argument("name")
    add.add_argument("--reset", action="store_true", help="a new code (the old one stops)")
    commands.add_parser("users", help="accounts and their roles per project")
    remove = commands.add_parser("remove-user", help="close an account now")
    remove.add_argument("name")
    remove.add_argument("--transfer-to", help="who takes over projects only they own")
    log = commands.add_parser("audit", help="who did what, newest first")
    log.add_argument("limit", nargs="?", type=int, default=50)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "host":
            result = host_projects()
        elif arguments.command == "add-user":
            if not _hosts_here():
                host_projects()
            result = add_account(arguments.name, reset=arguments.reset)
        elif arguments.command == "users":
            result = accounts()
        elif arguments.command == "remove-user":
            result = remove_account(arguments.name, transfer_to=arguments.transfer_to)
        else:
            result = hub_audit(arguments.limit)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
