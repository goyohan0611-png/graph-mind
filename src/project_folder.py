"""Project brains in a shared cloud folder: the same project brain, with no server and no network
setup. Everyone works the same from the office, home or abroad.

A project is one folder that its owner shares with the team in Google Drive, OneDrive or Dropbox,
the way people already share folders:

    <folder>/project.json        the project's id and its first owner's public key
    <folder>/log/<device>.jsonl   each PC appends only to its own file, so syncing never conflicts

Every line of a log is sealed with the project's key (the cloud service sees only ciphertext) and
signed by the PC that wrote it. Each PC replays every other PC's lines into a local cache and
checks each one itself, signature and rules alike, as the hub's database does: only an editor
publishes, only its author or an owner retracts a checkpoint, only an owner changes instructions
and members. A line that fails is skipped and noted, never applied.

People join with an invite code (gmp1.): it carries the project's key and the owner's public key
(so a joiner trusts no file in the folder that the owner did not sign) and a one-time secret
that proves the joining PC was invited. Removing someone ends their part in the project's
records; unsharing the folder in the cloud service ends their access to it.
"""
from __future__ import annotations

from pathlib import Path
import base64
import contextlib
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import uuid

import project_ledger as pl

PREFIX = "gmp1."
FORMAT = 1
RANK = {"viewer": 0, "editor": 1, "owner": 2}

# the ledger's writes, by name: a line names one of these and carries its parameters, never SQL
TEMPLATES = {
    "audit": "INSERT INTO ledger_audit(stamp, at, actor, action, target) VALUES(?,?,?,?,?)",
    "project": "INSERT INTO ledger_projects(name, created_by, created_at) VALUES(?,?,?)",
    "instructions": "UPDATE ledger_projects SET instructions=? WHERE name=?",
    "publish": "INSERT INTO ledger_checkpoints(id, project, stamp, created_at, status, payload) "
               "VALUES(?,?,?,?,?,?)",
    "retract": "UPDATE ledger_checkpoints SET status='retracted', payload=? WHERE id=?",
}
BY_SQL = {" ".join(sql.split()): name for name, sql in TEMPLATES.items()}

CACHE = """
CREATE TABLE IF NOT EXISTS folder_offsets(file TEXT PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS folder_devices(device TEXT PRIMARY KEY, member TEXT NOT NULL,
    role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS folder_invites(secret TEXT PRIMARY KEY, member TEXT NOT NULL,
    role TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS folder_roles(member TEXT PRIMARY KEY, role TEXT);
CREATE TABLE IF NOT EXISTS folder_rows(tbl TEXT NOT NULL, id TEXT NOT NULL, author TEXT NOT NULL,
    PRIMARY KEY(tbl, id));
CREATE TABLE IF NOT EXISTS folder_rejected(file TEXT, seq INTEGER, reason TEXT)
"""


# -- this PC's signing key ---------------------------------------------------------------------
def _device():
    """This PC's Ed25519 key (kept in the config), and its public half, base64."""
    from brain_log import _config, _save
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization
    stored = _config().get("device_key")
    if stored:
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(stored))
    else:
        private = Ed25519PrivateKey.generate()
        _save({"device_key": base64.b64encode(private.private_bytes(
            serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
            serialization.NoEncryption())).decode()})
    public = private.public_key().public_bytes(serialization.Encoding.Raw,
                                               serialization.PublicFormat.Raw)
    return private, base64.b64encode(public).decode()


def _verify(public_b64: str, signature_b64: str, message: bytes) -> bool:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    from cryptography.exceptions import InvalidSignature
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64)).verify(
            base64.b64decode(signature_b64), message)
        return True
    except (InvalidSignature, ValueError):
        return False


def _device_id(public_b64: str) -> str:
    return hashlib.sha256(public_b64.encode()).hexdigest()[:16]


# -- the ledger over a folder ---------------------------------------------------------------------
class FolderLedger(pl.Ledger):
    """One project in a shared folder: the ledger's own code over a local cache, with every write
    also appended (sealed, signed) to this PC's log there, and every other PC's lines replayed in
    after the same checks the hub's database makes."""

    def __init__(self, folder: Path, code: dict):
        from cryptography.fernet import Fernet
        self.folder, self.code = Path(folder), code
        self.project_id, self.owner_key = code["i"], code["o"]
        self.fernet = Fernet(code["k"].encode())
        self.private, self.public = _device()
        self.device = _device_id(self.public)
        cache = pl._home() / "folders" / f"{self.project_id}.sqlite"
        cache.parent.mkdir(parents=True, exist_ok=True)
        super().__init__(sqlite3.connect(cache, timeout=30), where=f"shared folder {self.folder}",
                         by="", role="")
        self.db.executescript(CACHE)
        self._pending, self._depth, self._replaying = [], 0, False
        self.sync()
        self.by = self._me()[0] or code.get("n") or pl.actor()

    # -- who is who -------------------------------------------------------------------------
    def _who(self, device: str):
        """(member, role) of a PC's key: the project's first owner, or a PC that joined."""
        if device == self.owner_key:
            row = self.db.execute("SELECT member FROM folder_devices WHERE device=?",
                                  (device,)).fetchone()
            return (row[0] if row else self.code.get("ow") or "owner"), "owner"
        row = self.db.execute("SELECT member, role FROM folder_devices WHERE device=?",
                              (device,)).fetchone()
        if not row:
            return None, None
        changed = self.db.execute("SELECT role FROM folder_roles WHERE member=?",
                                  (row[0],)).fetchone()
        return row[0], (changed[0] if changed else row[1])

    def _me(self):
        return self._who(self.public)

    def members(self, project) -> list[dict]:
        self._require(project)
        people = {}
        for device, member, role in self.db.execute(
                "SELECT device, member, role FROM folder_devices").fetchall():
            current = self._who(device)[1]
            if current:
                people[member] = max(people.get(member, current), current, key=RANK.get)
        return [{"member": m, "role": r} for m, r in sorted(people.items(),
                                                             key=lambda x: (-RANK[x[1]], x[0]))]

    # -- writes: checked, recorded, applied --------------------------------------------------
    def _sql(self, query, parameters=()):
        name = BY_SQL.get(" ".join(query.split()))
        if name is None or self._replaying:
            return super()._sql(query, parameters)
        member, role = self._me()
        refused = self._check(name, list(parameters), member, role)
        if refused:
            raise ValueError(f"NOT_ALLOWED: {refused}")
        result = super()._sql(query, parameters)
        self._remember(name, list(parameters), member)
        self._pending.append({"kind": "sql", "t": name, "p": list(parameters)})
        if not self._depth:
            self._flush()
        return result

    @contextlib.contextmanager
    def _atomic(self):
        self._depth += 1
        try:
            with super()._atomic():
                yield
        except BaseException:
            if self._depth == 1:
                self._pending.clear()                 # nothing of it reaches the folder
            raise
        finally:
            self._depth -= 1
        if not self._depth:
            self._flush()

    def _flush(self):
        records, self._pending = self._pending, []
        if records:
            self._append(records)

    def _append(self, bodies):
        """Lines at the end of this PC's own log, numbered after its last one."""
        import fasteners
        log = self.folder / "log" / f"{self.device}.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        with fasteners.InterProcessLock(str(pl._home() / "folders" / f"{self.project_id}.lock")):
            seq = 0
            if log.exists():
                lines = [l for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
                seq = json.loads(lines[-1])["seq"] if lines else 0
            out = []
            for body in bodies:
                seq += 1
                sealed = self.fernet.encrypt(json.dumps({**body, "at": pl._stamp()},
                                                        ensure_ascii=False).encode()).decode()
                message = f"{self.public}|{seq}|{sealed}".encode()
                out.append(json.dumps({"v": FORMAT, "dev": self.public, "seq": seq,
                                       "body": sealed, "sig": base64.b64encode(
                                           self.private.sign(message)).decode()}))
            with log.open("a", encoding="utf-8") as stream:
                stream.write("".join(line + "\n" for line in out))
                stream.flush()
                os.fsync(stream.fileno())
            # this PC applied them already: its own offset moves past them
            self.db.execute("INSERT INTO folder_offsets VALUES(?,?) ON CONFLICT(file) DO UPDATE "
                            "SET offset=excluded.offset", (log.name, log.stat().st_size))
            self.db.commit()

    def _remember(self, name, params, member):
        """Who wrote each checkpoint, for the rule that needs it later (retract)."""
        row = {"publish": ("ledger_checkpoints", 0)}.get(name)
        if row:
            self.db.execute("INSERT OR IGNORE INTO folder_rows VALUES(?,?,?)",
                            (row[0], params[row[1]], member or ""))

    def _author(self, table, row_id):
        row = self.db.execute("SELECT author FROM folder_rows WHERE tbl=? AND id=?",
                              (table, row_id)).fetchone()
        return row[0] if row else None

    def _check(self, name, params, member, role) -> str | None:
        """Why a write is refused, or None. The same rules the hub's database applies."""
        if not role:
            return "not a member of this project"
        rank = RANK[role]
        if name == "audit":
            return None
        if name == "project":
            return None if role == "owner" else "only the owner creates the project"
        if name == "instructions":
            return None if role == "owner" else "only the project's owner sets its instructions"
        if name == "publish":
            payload = json.loads(params[5])
            if rank < RANK["editor"]:
                return "a viewer cannot publish"
            return None if payload.get("actor") == member else "published in someone else's name"
        if name == "retract":
            author = self._author("ledger_checkpoints", params[1])
            return None if author == member or role == "owner" else (
                "only the person who published it, or the owner, can retract it")
        return "unknown change"

    def grant(self, project, member, role, by) -> dict:
        """A member already here gets the new role; anyone else gets an invite code."""
        self._require(project)
        if any(m["member"] == member for m in self.members(project)):
            return {**self.set_role(member, role, by), "project": project}
        return {"project": project, "member": member, "role": role,
                "invite_code": self.invite(member, role),
                "share": f"Share this folder with {member} in your cloud drive: {self.folder}"}

    def revoke(self, project, member, by) -> dict:
        self._require(project)
        self.set_role(member, None, by)
        return {"project": project, "member": member, "removed": True,
                "note": "Removed from the project's records now. Also stop sharing the folder "
                        "with them in your cloud drive, which is what ends their access to it."}

    # -- membership records -------------------------------------------------------------------
    def invite(self, member: str, role: str) -> str:
        """Owner only: a one-time code that lets a PC join as `member` with `role`."""
        if self._me()[1] != "owner":
            raise ValueError("NOT_ALLOWED: only the project's owner invites")
        if role not in RANK:
            raise ValueError("ROLE must be owner, editor or viewer")
        secret = secrets.token_urlsafe(18)
        self._apply_membership({"kind": "invite", "secret": secret, "member": member,
                                "role": role}, self.public)
        self._append([{"kind": "invite", "secret": secret, "member": member, "role": role}])
        return _encode({**self.code, "s": secret, "n": member})

    def set_role(self, member: str, role: str | None, by: str) -> dict:
        """Owner only: change a member's role, or remove them (role None)."""
        if self._me()[1] != "owner":
            raise ValueError("NOT_ALLOWED: only the project's owner changes members")
        owners = [m for m in self.members(self.names()[0]) if m["role"] == "owner"]
        if role != "owner" and [o["member"] for o in owners] == [member]:
            raise ValueError("LAST_OWNER: a project keeps at least one owner")
        body = {"kind": "role", "member": member, "role": role}
        self._apply_membership(body, self.public)
        self._append([body])
        return {"member": member, "role": role}

    def _join(self, secret: str):
        proof = hmac.new(secret.encode(), self.public.encode(), hashlib.sha256).hexdigest()
        body = {"kind": "join", "proof": proof, "secret_id": _secret_id(secret)}
        if self._apply_membership(body, self.public):
            self._append([body])
        return self._me()

    def _apply_membership(self, body, device) -> bool:
        member, role = self._who(device)
        if body["kind"] == "invite" and role == "owner":
            self.db.execute("INSERT OR IGNORE INTO folder_invites VALUES(?,?,?)",
                            (body["secret"], body["member"], body["role"]))
        elif body["kind"] == "role" and role == "owner":
            self.db.execute("INSERT INTO folder_roles VALUES(?,?) ON CONFLICT(member) DO UPDATE "
                            "SET role=excluded.role", (body["member"], body["role"]))
        elif body["kind"] == "join":
            for secret, invited, given in self.db.execute(
                    "SELECT secret, member, role FROM folder_invites").fetchall():
                if _secret_id(secret) == body.get("secret_id") and hmac.compare_digest(
                        hmac.new(secret.encode(), device.encode(), hashlib.sha256).hexdigest(),
                        body.get("proof", "")):
                    self.db.execute("INSERT OR IGNORE INTO folder_devices VALUES(?,?,?)",
                                    (device, invited, given))
                    break
            else:
                return False
        elif body["kind"] == "name" and device == self.owner_key:
            self.db.execute("INSERT OR IGNORE INTO folder_devices VALUES(?,?,?)",
                            (device, body["member"], "owner"))
        else:
            return False
        self.db.commit()
        return True

    # -- reading the others ---------------------------------------------------------------------
    def sync(self) -> int:
        """Replay every PC's new lines, oldest first; a line whose row has not arrived yet waits
        (its file's offset stays before it) and is tried again next time."""
        import fasteners
        logs = self.folder / "log"
        if not logs.is_dir():
            return 0
        applied = 0
        with fasteners.InterProcessLock(str(pl._home() / "folders" / f"{self.project_id}.lock")):
            fresh = []
            for log in sorted(logs.glob("*.jsonl")):
                start = (self.db.execute("SELECT offset FROM folder_offsets WHERE file=?",
                                         (log.name,)).fetchone() or [0])[0]
                with log.open("rb") as stream:
                    stream.seek(start)
                    data = stream.read()
                position = start
                for raw in data.split(b"\n")[:-1]:       # a last line without \n is unfinished
                    position += len(raw) + 1
                    if raw.strip():
                        fresh.append((log.name, position, raw))
            # each PC's lines in its own order; rounds until nothing more applies, so an answer
            # waiting for a request from another PC's file goes in as soon as that request does
            queues = {}
            for name, position, raw in fresh:
                queues.setdefault(name, []).append((position, raw))
            progress = True
            while progress:
                progress = False
                for name, queue in queues.items():
                    while queue:
                        position, raw = queue[0]
                        outcome = self._replay(raw)
                        if outcome == "wait":
                            break
                        if outcome is not True:
                            self.db.execute("INSERT INTO folder_rejected VALUES(?,?,?)",
                                            (name, json.loads(raw).get("seq"), outcome))
                        applied += outcome is True
                        self.db.execute("INSERT INTO folder_offsets VALUES(?,?) ON CONFLICT(file) "
                                        "DO UPDATE SET offset=excluded.offset", (name, position))
                        queue.pop(0)
                        progress = True
            self.db.commit()
        return applied

    def _replay(self, raw: bytes):
        """True when applied, "wait" when it depends on a row not here yet, else the reason it
        was refused."""
        try:
            line = json.loads(raw)
            message = f"{line['dev']}|{line['seq']}|{line['body']}".encode()
            if not _verify(line["dev"], line["sig"], message):
                return "bad signature"
            body = json.loads(self.fernet.decrypt(line["body"].encode()))
        except Exception as error:                     # noqa: BLE001 (any malformed line)
            return f"unreadable: {type(error).__name__}"
        if body["kind"] != "sql":
            if self._apply_membership(body, line["dev"]):
                return True
            # a PC's joining can arrive before the owner's invite has been replayed here
            return "wait" if body["kind"] == "join" else "not allowed"
        member, role = self._who(line["dev"])
        if body.get("t") not in TEMPLATES:
            return "unknown change"
        refused = self._check(body["t"], body["p"], member, role)
        # its writer's joining may be in a line this PC has not replayed yet. An honest PC never
        # writes a line its own check refuses, so a line that waits for good holds up only its
        # writer.
        if refused and not role:
            return "wait"
        if refused:
            return refused
        self._replaying = True
        try:
            super()._sql(TEMPLATES[body["t"]], body["p"])
            self._remember(body["t"], body["p"], member)
        except sqlite3.IntegrityError:
            pass                                        # applied before (the same line again)
        finally:
            self._replaying = False
        return True


def _secret_id(secret: str) -> str:
    return hashlib.sha256(("invite|" + secret).encode()).hexdigest()[:16]


def _encode(value: dict) -> str:
    return PREFIX + base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def decode(code: str) -> dict:
    body = code.strip()[len(PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return {key: value[key] for key in ("i", "k", "o", "p")} | {
            key: value.get(key) for key in ("s", "n", "ow")}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("INVALID_PROJECT_CODE: a shared-folder project code starts with gmp1."
                         ) from error


# -- where shared folders live ------------------------------------------------------------------
def cloud_roots() -> list[Path]:
    """The folders the PC's cloud drives sync (Google Drive, OneDrive, Dropbox), as found, or
    those GRAPH_MIND_CLOUD_ROOTS names (separated like PATH; empty: none)."""
    if "GRAPH_MIND_CLOUD_ROOTS" in os.environ:
        return [Path(p) for p in os.environ["GRAPH_MIND_CLOUD_ROOTS"].split(os.pathsep) if p]
    home = Path.home()
    found = []
    for name in ("OneDriveConsumer", "OneDriveCommercial", "OneDrive"):
        if os.environ.get(name):
            found.append(Path(os.environ[name]))
    for letter in "GHIJKLMNOPQRSTUVWXYZ":
        for drive in ("내 드라이브", "My Drive", "공유 드라이브", "Shared drives"):
            found.append(Path(f"{letter}:/{drive}"))
    found += [home / "Google Drive", home / "My Drive", home / "Dropbox",
              *(home / "Library" / "CloudStorage").glob("*")]
    seen, out = set(), []
    for path in found:
        try:
            if path.is_dir() and str(path).casefold() not in seen:
                seen.add(str(path).casefold())
                out.append(path)
        except OSError:
            continue
    return out


def find_folder(project_id: str, hint: str | None = None) -> Path | None:
    """The synced copy of a shared project folder on this PC, by its id (folders keep their name
    when shared, but the place differs per person and per cloud service)."""
    candidates = [Path(hint)] if hint else []
    for root in cloud_roots():
        candidates += [root / "Graph-MIND" / p.name for p in (root / "Graph-MIND").glob("*")] \
            if (root / "Graph-MIND").is_dir() else []
        candidates += list(root.glob("*")) + list(root.glob("*/*"))
    for folder in candidates:
        try:
            info = json.loads((folder / "project.json").read_text(encoding="utf-8"))
            if info.get("id") == project_id:
                return folder
        except (OSError, ValueError):
            continue
    return None


# -- making and joining ---------------------------------------------------------------------------
def create(name: str, by: str, where: Path | None = None) -> dict:
    """A new project in a cloud drive's folder (Graph-MIND/<name>), owned by this PC's person."""
    from cryptography.fernet import Fernet
    name = pl._project_name(name)
    roots = [where] if where else cloud_roots()
    if not roots:
        raise ValueError("NO_CLOUD_FOLDER: no Google Drive, OneDrive or Dropbox folder found on "
                         "this PC. Install one, or give the folder to use.")
    folder = Path(roots[0]) / "Graph-MIND" / name
    if (folder / "project.json").exists():
        raise ValueError(f"PROJECT_NAME_TAKEN: {folder} already holds a project")
    _, public = _device()
    code = {"i": uuid.uuid4().hex, "k": Fernet.generate_key().decode(), "o": public,
            "p": name, "ow": by}
    (folder / "log").mkdir(parents=True, exist_ok=True)
    (folder / "project.json").write_text(json.dumps(
        {"format": FORMAT, "id": code["i"], "name": name, "owner_key": public,
         "note": "Graph-MIND project brain. Share this whole folder with your team; its "
                 "contents are sealed, and only an invite code opens them."},
        ensure_ascii=False, indent=2), encoding="utf-8")
    _remember(folder, code)
    book = FolderLedger(folder, code)
    try:
        book._apply_membership({"kind": "name", "member": by}, public)
        book._append([{"kind": "name", "member": by}])
        book.by = by
        book.create_project(name, by)
    finally:
        book.db.close()
    return {"project": name, "folder": str(folder), "created": True, "owner": by,
            "share": f"Share this folder with your team in your cloud drive: {folder}"}


def join(code_text: str, folder: str | None = None) -> dict:
    """On a teammate's PC: find the shared folder (already synced here) and join it."""
    code = decode(code_text)
    found = find_folder(code["i"], folder)
    if found is None:
        raise ValueError("FOLDER_NOT_FOUND: the shared folder is not on this PC yet. In Google "
                         "Drive, open 'Shared with me', right-click the project folder and choose "
                         "'Add shortcut to Drive' (OneDrive: 'Add shortcut to My files'), wait for "
                         "it to sync, then join again (or give the folder's path).")
    _remember(found, {key: code[key] for key in ("i", "k", "o", "p", "ow")})
    book = FolderLedger(found, code)
    try:
        member, role = book._me()
        if not role and code.get("s"):
            member, role = book._join(code["s"])
        if not role:
            raise ValueError("NOT_INVITED: this code does not admit this PC (used up, revoked, or "
                             "the owner's invite has not synced here yet: try again shortly)")
        return {"joined_as": member, "role": role, "project": code["p"], "folder": str(found)}
    finally:
        book.db.close()


def _remember(folder: Path, code: dict):
    from brain_log import _config, _save
    kept = [entry for entry in _config().get("folder_projects", [])
            if entry["code"]["i"] != code["i"]]
    _save({"folder_projects": kept + [{"path": str(folder), "code": code}]})


def ledgers() -> tuple[list, list[str]]:
    """Every shared-folder project this PC takes part in; folders not synced here are reported."""
    from brain_log import _config
    found, missing = [], []
    for entry in _config().get("folder_projects", []):
        folder = Path(entry["path"])
        if not (folder / "project.json").exists():
            folder = find_folder(entry["code"]["i"]) or folder
        if not (folder / "project.json").exists():
            missing.append(f"shared folder {entry['code']['p']} (not synced on this PC)")
            continue
        found.append(FolderLedger(folder, entry["code"]))
    return found, missing


def signature() -> tuple:
    """Changes in any shared project's logs (for notifications without a server to signal)."""
    from brain_log import _config
    marks = []
    for entry in _config().get("folder_projects", []):
        for log in sorted((Path(entry["path"]) / "log").glob("*.jsonl")):
            try:
                stat = log.stat()
                marks.append((log.name, stat.st_size, stat.st_mtime_ns))
            except OSError:
                continue
    return tuple(marks)
