"""One brain across devices: the shared folder holds the memory, each machine holds an index.

A database file in a synced folder breaks: OneDrive and Dropbox copy files, they do not order two
machines' writes. So the memory itself is kept as plain append-only text in the shared folder, and
every machine builds its own SQLite index from it:

    <GRAPH_MIND_FOLDER>/log/
      desktop.jsonl        <- only the desktop ever appends here
      laptop.jsonl         <- only the laptop ever appends here

Each line is one record (a memory, or one conversation turn), never edited, never removed. A file
has exactly one writer, so syncing cannot interleave two machines' writes, and a line half-written
or half-synced is simply not read until its newline arrives. Reading imports every device's new
lines into the local index, in the order they were written, so a memory on the laptop can
supersede one written on the desktop: by then both are in the same database.

The index lives outside the folder and can be deleted at any time; `sync` rebuilds it.

The same log can live in Postgres instead of a folder ("memory_folder": "local" or a postgres://
URL). A synced folder reaches other PCs when the sync client gets round to it, often a minute
later; a Postgres table is there the moment the row commits, so a PC asking right after another
answered already sees it. "local" runs Postgres on this PC (pgserver, no setup); other PCs reach
it over Tailscale with its postgres:// URL. Rows are only ever inserted, one writer at a time
(an advisory lock), so reading by sequence number never skips a row committed late.

    python brain_log.py            # self-check with two simulated devices
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
import base64
import json
import os
import platform
import re
import sqlite3

KINDS = ("memory", "turn")


def device_name() -> str:
    """This machine's log name. GRAPH_MIND_DEVICE overrides it (tests, two accounts on one PC)."""
    raw = os.environ.get("GRAPH_MIND_DEVICE") or platform.node() or "device"
    return re.sub(r"[^a-z0-9]+", "-", raw.casefold()).strip("-") or "device"


def config_path() -> Path:
    """Where the chosen brain folder is remembered for every client on this machine."""
    override = os.environ.get("GRAPH_MIND_CONFIG")
    return Path(override) if override else Path.home() / ".graph-mind" / "config.json"


def _config() -> dict:
    try:
        return json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def is_database(shared) -> bool:
    return isinstance(shared, str) and shared.startswith(("postgres://", "postgresql://"))


_LOCAL_URI = {}
_JOINED = {}
PORT = int(os.environ.get("GRAPH_MIND_PG_PORT", "54329"))   # fixed, so a connection code stays valid
CODE_PREFIX = "gm1."
TAILSCALE = "100.64.0.0/10"


def _pg_data() -> Path:
    from development_paths import graph_mind_home
    return graph_mind_home() / "postgres"


def local_postgres() -> str:
    """This PC's own Postgres, started on first use and left running for every other client
    (Claude, Codex and the capture service find the running server and share it).

    pgserver supplies the binaries; the server is started here rather than by pgserver because
    pgserver picks a new port on every start and trusts every connection, and a PC that shares
    its brain needs a port that stays put and a password for anyone not on this PC. The memory
    lives in database "graphmind" under role "graphmind", which is not a superuser: that is the
    role other PCs log in as."""
    if "uri" not in _LOCAL_URI:
        import fasteners
        from pgserver._commands import initdb, pg_ctl
        from pgserver.utils import PostmasterInfo
        data = _pg_data()
        data.parent.mkdir(parents=True, exist_ok=True)
        with fasteners.InterProcessLock(str(data.parent / "postgres.lock")):
            fresh = not (data / "PG_VERSION").exists()
            if fresh:
                initdb(["--auth=trust", "--encoding=utf8", "-U", "postgres"], pgdata=data)
            running = PostmasterInfo.read_from_pgdata(data)
            if running is None or not running.is_running():
                pg_ctl(["-w", "-o", f"-p {PORT}", "-l", str(data / "log"), "start"],
                       pgdata=data, timeout=60)
            if fresh:
                import psycopg
                with psycopg.connect(_admin_uri(), autocommit=True) as admin:
                    admin.execute("CREATE ROLE graphmind LOGIN")
                    admin.execute("CREATE DATABASE graphmind OWNER graphmind")
        _LOCAL_URI["uri"] = f"postgresql://graphmind@127.0.0.1:{PORT}/graphmind"
    return _LOCAL_URI["uri"]


def _admin_uri() -> str:
    return f"postgresql://postgres@127.0.0.1:{PORT}/postgres"


def share(open_firewall: bool = True) -> dict:
    """Let the user's other PCs in: the ones on the same network, and the ones on Tailscale.

    Postgres starts listening beyond this PC, role "graphmind" gets a password, and only that
    role, only for its own database, may log in from elsewhere (and only with the password);
    this PC itself still connects without one. The result is a connection code that carries the
    addresses, the port and the password, so the other PC's user pastes one line and types none
    of it. The same code comes back every time; it is as good as the password, so it is shown
    to the user and never posted anywhere."""
    import psycopg
    import secrets
    from pgserver._commands import pg_ctl
    local_postgres()
    password = _config().get("share_password") or secrets.token_urlsafe(18)
    data = _pg_data()
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        # token_urlsafe is [A-Za-z0-9_-]: nothing in it can close the quoted literal
        admin.execute(f"ALTER ROLE graphmind PASSWORD '{password}'")
        restart = admin.execute("SHOW listen_addresses").fetchone()[0] != "*"
        if restart:
            admin.execute("ALTER SYSTEM SET listen_addresses = '*'")
    hba = data / "pg_hba.conf"
    rule = "host graphmind graphmind all scram-sha-256"
    if rule not in hba.read_text(encoding="utf-8"):
        with hba.open("a", encoding="utf-8") as stream:
            stream.write("\n# Graph-MIND: other PCs, with the password\n" + rule + "\n")
    pg_ctl(["-w", "-m", "fast", "restart"] if restart else ["reload"], pgdata=data, timeout=60)
    set_folder("local")
    _save({"share_password": password})
    hosts = _addresses()
    code = CODE_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": hosts, "p": PORT, "k": password}).encode()).decode().rstrip("=")
    return {"connection_code": code, "addresses": hosts, "port": PORT,
            "firewall": _firewall() if open_firewall else "not changed"}


def _addresses() -> list[str]:
    """This PC's IPv4 addresses other PCs can use: the local network first, then Tailscale."""
    import ipaddress
    import psutil
    import socket
    up = {name for name, stats in psutil.net_if_stats().items() if stats.isup}
    found = {entry.address for name, entries in psutil.net_if_addrs().items() if name in up
             for entry in entries if entry.family == socket.AF_INET}
    usable = [a for a in found if not (ipaddress.ip_address(a).is_loopback
                                       or ipaddress.ip_address(a).is_link_local)]
    tailscale = ipaddress.ip_network(TAILSCALE)
    return sorted(usable, key=lambda a: (ipaddress.ip_address(a) in tailscale,
                                         ipaddress.ip_address(a)))


def _firewall() -> str:
    """Windows blocks the port until a rule allows it. The rule admits only the local network
    and Tailscale, and adding it asks the user's permission once (an administrator prompt)."""
    if platform.system() != "Windows":
        return "not needed"
    import subprocess
    name = "Graph-MIND-Postgres"

    def exists():
        return subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                              capture_output=True).returncode == 0
    if exists():
        return "open"
    arguments = (f"advfirewall firewall add rule name={name} dir=in action=allow "
                 f"protocol=TCP localport={PORT} remoteip=LocalSubnet,{TAILSCALE}")
    subprocess.run(["powershell", "-NoProfile", "-Command",
                    f"Start-Process netsh -Verb RunAs -Wait -WindowStyle Hidden "
                    f"-ArgumentList '{arguments}'"], capture_output=True)
    return "open" if exists() else "blocked: the administrator prompt was declined"


def _decode(code: str) -> dict:
    body = code.strip()[len(CODE_PREFIX):]
    try:
        value = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return {"h": [str(h) for h in value["h"]], "p": int(value["p"]), "k": str(value["k"])}
    except (ValueError, KeyError, TypeError) as error:
        raise ValueError("INVALID_CONNECTION_CODE") from error


def _joined(code: str) -> str:
    """The URL for a connection code: the first of its addresses that answers right now, so a
    laptop reaches the same brain over the office network and over Tailscale from home."""
    import socket
    import time
    import urllib.parse
    cached = _JOINED.get(code)
    if cached and time.monotonic() - cached[0] < 300:
        return cached[1]
    value = _decode(code)
    host = value["h"][0] if value["h"] else "127.0.0.1"
    for candidate in value["h"]:
        try:
            socket.create_connection((candidate, value["p"]), timeout=1.5).close()
            host = candidate
            break
        except OSError:
            continue
    url = (f"postgresql://graphmind:{urllib.parse.quote(value['k'], safe='')}"
           f"@{host}:{value['p']}/graphmind")
    _JOINED[code] = (time.monotonic(), url)
    return url


def folder():
    """Where the shared brain lives: a folder (Path), or a Postgres URL (str). GRAPH_MIND_FOLDER
    if set, otherwise the one chosen in conversation; "local" means this PC's own Postgres and a
    connection code means another PC's."""
    value = os.environ.get("GRAPH_MIND_FOLDER") or _config().get("memory_folder")
    if not value:
        return None
    if value == "local":
        return local_postgres()
    if value.startswith(CODE_PREFIX):
        return _joined(value)
    return value if is_database(value) else Path(value).expanduser()


def _save(changes: dict) -> None:
    settings = {**_config(), **changes}
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def set_folder(path) -> Path:
    """Choose the brain folder for this machine. A synced folder (OneDrive, Google Drive, Dropbox)
    makes the same brain available on every PC that chooses it too. "local" is this PC's own
    Postgres; a connection code (from `share` on another PC) joins that PC's."""
    path = str(path).strip()
    if path == "local" or is_database(path):
        chosen = path
    elif path.startswith(CODE_PREFIX):
        chosen = path
        _decode(chosen)                          # refuse a mangled code before saving it
    else:
        chosen = Path(path).expanduser().resolve()
        (chosen / "log").mkdir(parents=True, exist_ok=True)
    _save({"memory_folder": str(chosen)})
    return chosen


def append(shared: Path, kind: str, record: dict) -> None:
    """Add one record to this device's log. Only ever appends, and flushes it to disk."""
    append_many(shared, [(kind, record)])


def append_many(shared: Path, items) -> int:
    """Append several records in one write and one flush (an export can be thousands)."""
    lines = []
    for kind, record in items:
        if kind not in KINDS:
            raise ValueError("UNKNOWN_LOG_KIND")
        lines.append(json.dumps({"kind": kind, "device": device_name(),
                                 "written_at": datetime.now().isoformat(timespec="microseconds"),
                                 "record": record}, ensure_ascii=False, sort_keys=True))
    if not lines:
        return 0
    if is_database(shared):
        return _pg_append(shared, lines)
    log = Path(shared).expanduser() / "log" / f"{device_name()}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return len(lines)


def export_history(shared: Path, index: Path) -> dict:
    """Put what this machine remembered before the folder existed into its log, once.

    This uploads existing memories to wherever the folder syncs, so it runs only when the user
    asks for it. Each record is marked as exported and never written twice; memories go out in
    the order they were stored so a replacement always follows what it replaces.
    """
    db = _cursor_db(index)
    db.row_factory = sqlite3.Row
    try:
        db.execute("CREATE TABLE IF NOT EXISTS brain_log_exported("
                   "kind TEXT NOT NULL, id TEXT NOT NULL, PRIMARY KEY(kind, id))")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        done = {(row[0], row[1]) for row in db.execute("SELECT kind, id FROM brain_log_exported")}
        items = []
        if "local_brain_memories" in tables:
            for row in db.execute("SELECT * FROM local_brain_memories ORDER BY revision"):
                if ("memory", row["memory_id"]) in done:
                    continue
                items.append(("memory", {
                    "memory_id": row["memory_id"], "scope": row["scope"],
                    "memory_type": row["memory_type"], "title": row["title"],
                    "content": row["content"], "effective_at": row["effective_at"],
                    "known_at": row["known_at"], "actor": row["actor"],
                    "tags": json.loads(row["tags_json"]),
                    "entities": json.loads(row["entities_json"]),
                    "provenance": json.loads(row["provenance_json"]),
                    "supersedes_memory_id": row["supersedes_memory_id"]}))
        if "conversation_turns" in tables:
            for row in db.execute("SELECT * FROM conversation_turns ORDER BY revision"):
                if ("turn", row["turn_id"]) in done:
                    continue
                items.append(("turn", {key: row[key] for key in (
                    "turn_id", "client", "client_session_id", "scope", "cwd", "happened_at",
                    "known_at", "role", "content_redacted", "raw_sha256", "redaction_count",
                    "source_path", "source_ordinal")}))
        written = append_many(shared, items)
        with db:
            db.executemany("INSERT OR IGNORE INTO brain_log_exported VALUES(?,?)",
                           [(kind, record["memory_id"] if kind == "memory" else record["turn_id"])
                            for kind, record in items])
        return {"memories": sum(kind == "memory" for kind, _ in items),
                "turns": sum(kind == "turn" for kind, _ in items), "lines_written": written}
    finally:
        db.close()


def _cursor_db(index: Path) -> sqlite3.Connection:
    db = sqlite3.connect(index)
    db.executescript("""
      CREATE TABLE IF NOT EXISTS brain_log_cursor(
        source TEXT PRIMARY KEY, offset INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS brain_log_pending(
        line TEXT PRIMARY KEY);""")
    return db


def _pg(url):
    import psycopg
    connection = psycopg.connect(url, autocommit=False)
    connection.execute("""CREATE TABLE IF NOT EXISTS brain_log(
        seq BIGSERIAL PRIMARY KEY, device TEXT NOT NULL, line TEXT NOT NULL)""")
    connection.commit()
    return connection


def _pg_append(url, lines) -> int:
    with _pg(url) as connection:
        # one writer at a time: a row can then never commit after a later sequence number has
        # been read, which would make a reader's cursor step over it for good
        connection.execute("SELECT pg_advisory_xact_lock(4242)")
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO brain_log(device, line) VALUES (%s, %s)",
                               [(device_name(), line) for line in lines])
        connection.commit()
    return len(lines)


def devices(shared) -> list[str]:
    """Every device that has written to this brain."""
    if is_database(shared):
        with _pg(shared) as connection:
            return [row[0] for row in connection.execute(
                "SELECT DISTINCT device FROM brain_log ORDER BY device")]
    return sorted(p.stem for p in (Path(shared) / "log").glob("*.jsonl"))


def _new_lines(db: sqlite3.Connection, shared) -> list[str]:
    """Complete lines added to any device's log since this machine last read it."""
    if is_database(shared):
        source = "postgres"
        row = db.execute("SELECT offset FROM brain_log_cursor WHERE source=?",
                         (source,)).fetchone()
        with _pg(shared) as connection:
            rows = connection.execute("SELECT seq, line FROM brain_log WHERE seq > %s ORDER BY seq",
                                      (row[0] if row else 0,)).fetchall()
        if rows:
            with db:
                db.execute("INSERT OR REPLACE INTO brain_log_cursor VALUES(?,?)",
                           (source, rows[-1][0]))
        return [line for _, line in rows]
    lines = []
    for log in sorted((Path(shared) / "log").glob("*.jsonl")):
        row = db.execute("SELECT offset FROM brain_log_cursor WHERE source=?",
                         (log.name,)).fetchone()
        start = row[0] if row else 0
        if log.stat().st_size < start:      # replaced by the sync client: re-read, imports are idempotent
            start = 0
        with log.open("rb") as handle:
            handle.seek(start)
            data = handle.read()
        complete = data[:data.rfind(b"\n") + 1]        # an unfinished last line waits for its newline
        lines += [l for l in complete.decode("utf-8", "replace").splitlines() if l.strip()]
        with db:
            db.execute("INSERT OR REPLACE INTO brain_log_cursor VALUES(?,?)",
                       (log.name, start + len(complete)))
    return lines


def sync(shared: Path, index: Path) -> dict:
    """Bring this machine's index up to date with every device's log."""
    from conversation_memory import ConversationMemoryStore
    from local_brain import LocalBrainStore

    db = _cursor_db(index)
    try:
        pending = [row[0] for row in db.execute("SELECT line FROM brain_log_pending")]
        lines = pending + _new_lines(db, shared)
        items = []
        for line in lines:
            try:
                items.append((json.loads(line), line))
            except ValueError:
                continue                                  # a corrupt line is skipped, not fatal
        items.sort(key=lambda pair: pair[0].get("written_at", ""))
        imported, conflicts, waiting = 0, 0, []
        with LocalBrainStore(index) as brain, ConversationMemoryStore(index) as turns:
            # A supersession can arrive before the memory it replaces when two devices' logs are
            # read in one batch; keep retrying while anything is still landing.
            while items:
                progress, retry = False, []
                for item, line in items:
                    try:
                        if item["kind"] == "memory":
                            outcome = brain.remember(item["record"])
                        else:
                            outcome = turns.record(item["record"])
                    except ValueError as error:
                        if str(error) == "SUPERSEDED_LOCAL_MEMORY_NOT_FOUND":
                            retry.append((item, line))
                            continue
                        conflicts += 1                       # e.g. an id reused with other content
                        continue
                    progress = True
                    imported += outcome.get("disposition") != "ALREADY_RECORDED"
                items = retry
                if not progress:
                    waiting = [line for _, line in retry]
                    break
        with db:
            db.execute("DELETE FROM brain_log_pending")
            db.executemany("INSERT OR IGNORE INTO brain_log_pending VALUES(?)",
                           [(line,) for line in waiting])
        return {"imported": imported, "conflicts": conflicts, "waiting": len(waiting)}
    finally:
        db.close()


def _self_check():
    import tempfile

    root = Path(tempfile.mkdtemp())
    shared, desk_index, lap_index = root / "OneDrive" / "Graph-MIND", root / "desk.sqlite", \
        root / "lap.sqlite"
    from conversation_memory import ConversationMemoryStore
    from local_brain import LocalBrainStore

    def memory(mid, text, when, supersedes=None):
        return {"memory_id": mid, "scope": "global", "memory_type": "fact", "title": text,
                "content": text, "effective_at": when, "known_at": when, "actor": "assistant",
                "tags": [], "entities": ["사는 곳"],
                "provenance": {"source_type": "test", "source_ref": "self-check"},
                "supersedes_memory_id": supersedes}

    os.environ["GRAPH_MIND_DEVICE"] = "desktop"
    append(shared, "memory", memory("m1", "사는 곳: 서울", "2026-03-01T09:00:00"))
    append(shared, "turn", {"turn_id": "t1", "client": "claude-code", "client_session_id": "s",
                            "scope": "global", "role": "user",
                            "content_redacted": "콩이를 제주도에 데려갔어", "raw_sha256": "x",
                            "source_path": "p", "happened_at": "2026-03-01T09:00:00",
                            "known_at": "2026-03-01T09:00:00", "source_ordinal": 1})
    os.environ["GRAPH_MIND_DEVICE"] = "laptop"
    # the laptop has not seen the desktop yet, so this lands in its log first in file order
    append(shared, "memory", memory("m2", "사는 곳: 부산", "2026-10-01T09:00:00", supersedes="m1"))

    first = sync(shared, desk_index)
    assert first == {"imported": 3, "conflicts": 0, "waiting": 0}, first
    assert sync(shared, desk_index)["imported"] == 0, "re-sync imports nothing twice"

    with LocalBrainStore(desk_index) as brain:
        found = brain.recall("사는 곳", as_of="2026-12-01T00:00:00")
    assert [m["memory_id"] for m in found["matches"]] == ["m2"], found
    with ConversationMemoryStore(desk_index) as turns:
        assert turns.search("콩이")["status"] == "KNOWN", "the turn from the other device arrived"

    log = shared / "log" / "laptop.jsonl"
    with log.open("a", encoding="utf-8") as stream:
        stream.write('{"kind": "memory", "half": ')       # a line still syncing: no newline yet
    assert sync(shared, lap_index)["imported"] == 3, "the torn line is not read"
    assert sync(shared, lap_index)["conflicts"] == 0

    os.environ.pop("GRAPH_MIND_DEVICE")
    print("brain_log self-check ok")


if __name__ == "__main__":
    _self_check()
