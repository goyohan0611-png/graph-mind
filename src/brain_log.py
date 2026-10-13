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
answered already sees it. "local" runs Postgres on this PC (fetched once, no setup); other PCs reach
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
import subprocess

KINDS = ("memory", "turn", "forget")   # forget: ids another PC erased (forget.py)


def device_name() -> str:
    """This machine's log name. GRAPH_MIND_DEVICE overrides it (tests, two accounts on one PC)."""
    raw = os.environ.get("GRAPH_MIND_DEVICE") or platform.node() or "device"
    return re.sub(r"[^a-z0-9]+", "-", raw.casefold()).strip("-") or "device"


def config_path() -> Path:
    """Where the chosen brain folder is remembered for every client on this machine."""
    override = os.environ.get("GRAPH_MIND_CONFIG")
    if override:
        return Path(override)
    from development_paths import graph_mind_home
    return graph_mind_home() / "config.json"            # ~/.graph-mind unless GRAPH_MIND_HOME


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

    The server is started here on a port that stays put, with a password for anyone not on this
    PC (postgres_bin says where its programs come from). The memory lives in database
    "graphmind" under role "graphmind", which is not a superuser: that is the role other PCs log
    in as."""
    data = _pg_data()
    # Checked on every call, not once per process: a server that stopped (reboot, a crash, a
    # console closing on it) is started again by whoever needs it next, instead of every
    # client failing until someone notices. A server still running on programs from before
    # (pgserver's, built without TLS) is restarted on the current ones, once.
    if not _current(_server_exe(data)):
        import fasteners
        data.parent.mkdir(parents=True, exist_ok=True)
        with fasteners.InterProcessLock(str(data.parent / "postgres.lock")):
            fresh = not (data / "PG_VERSION").exists()
            if fresh:
                _initdb_with_retries(data)
            running = _server_exe(data)
            if running and not _current(running):
                _pg_tool("pg_ctl", ["-D", data, "-w", "-m", "fast", "stop"])
            if not _current(_server_exe(data)):
                _start(data, PORT)
            if fresh:
                import psycopg
                with psycopg.connect(_admin_uri(), autocommit=True) as admin:
                    admin.execute("CREATE ROLE graphmind LOGIN")
                    admin.execute("CREATE DATABASE graphmind OWNER graphmind")
    _LOCAL_URI["uri"] = f"postgresql://graphmind@127.0.0.1:{PORT}/graphmind"
    # project brains' tables and file functions, once per process: a teammate or the owner's
    # other PC may use them without this PC ever opening a project itself
    try:
        from project_ledger import prepare_hub
        prepare_hub(_LOCAL_URI["uri"])
    except Exception:
        pass                                  # the personal memory works without them
    return _LOCAL_URI["uri"]


# Postgres's own programs, built with OpenSSL for every machine a hub runs on: Windows, macOS,
# and Linux on x86-64 and 64-bit ARM (a NAS, a Raspberry Pi), from
# theseus-rs/postgresql-binaries (the Windows build is EnterpriseDB's). They are fetched once,
# the first time a machine hosts,
# checked against the hashes pinned here and kept in the user's cache folder, apart from any
# memory; every Graph-MIND home on the machine shares them. Nothing here is a Python package, so
# hosting works on any Python. Same major version as before (16): existing data is used as is.
POSTGRES = "16.14.0"
_POSTGRES_BUILDS = {
    ("Windows", "x86_64"): ("x86_64-pc-windows-msvc.zip",
                            "69f1cf5060ab99190f98604312b09d80a691cbcd1836bd4a2eb2067a7ad9a89f"),
    ("Linux", "x86_64"): ("x86_64-unknown-linux-gnu.tar.gz",
                          "3541705c9ca2fdbb0bfc7562b10a36aac155f18d45577d8ec6725b874ccada4a"),
    ("Linux", "aarch64"): ("aarch64-unknown-linux-gnu.tar.gz",
                           "b266719b02d7e1dd7c9e74ec129d6443ba1f4bb2f13ff71e6e75fde459d49b47"),
    ("Darwin", "x86_64"): ("x86_64-apple-darwin.tar.gz",
                           "c5ecdea2528e29503140e259c043002f6f8f2e9d1ee2f1decb44e8b394254820"),
    ("Darwin", "aarch64"): ("aarch64-apple-darwin.tar.gz",
                            "a7a4846456df26d27f815267dfe725b4ad4f46312c032e7b5939468250a4891c"),
}
_MACHINES = {"amd64": "x86_64", "x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}


def postgres_bin() -> Path:
    """The folder holding this machine's Postgres programs, fetched the first time."""
    build = _POSTGRES_BUILDS.get((platform.system(),
                                  _MACHINES.get(platform.machine().lower(), "")))
    if build is None:
        raise RuntimeError(f"Hosting a hub on {platform.system()} {platform.machine()} is not "
                           "supported yet (Windows, macOS, and Linux on x86-64 or 64-bit ARM "
                           "are). Joining a hub another machine hosts works here.")
    from platformdirs import user_cache_dir
    root = Path(os.environ.get("GRAPH_MIND_RUNTIME")
                or user_cache_dir("graph-mind", appauthor=False)) / f"postgresql-{POSTGRES}"
    if (root / "bin" / _exe("postgres")).exists():
        return root / "bin"
    import hashlib
    import io
    import shutil
    import tarfile
    import urllib.request
    import uuid
    import zipfile
    name = f"postgresql-{POSTGRES}-{build[0]}"
    with urllib.request.urlopen("https://github.com/theseus-rs/postgresql-binaries/releases/"
                                f"download/{POSTGRES}/{name}", timeout=600) as response:
        data = response.read()
    if hashlib.sha256(data).hexdigest() != build[1]:
        raise RuntimeError("The Postgres download did not match its pinned hash; nothing was "
                           "installed. Try again later.")
    partial = root.with_name(f"{root.name}.partial-{uuid.uuid4().hex[:8]}")
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            archive.extractall(partial)
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            if hasattr(tarfile, "data_filter"):
                archive.extractall(partial, filter="data")    # no path leaves the folder
            else:
                archive.extractall(partial)                   # ponytail: Python <3.10.12
    inner = partial / name.removesuffix(".zip").removesuffix(".tar.gz")
    try:
        os.replace(inner if inner.is_dir() else partial, root)
    except OSError:
        if not (root / "bin").is_dir():                       # not another process's copy
            raise
    shutil.rmtree(partial, ignore_errors=True)
    return root / "bin"


def _exe(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def _pg_tool(name: str, args: list, timeout: int = 120) -> str:
    """Run one of Postgres's programs, hidden, and return what it printed. The output goes to a
    file, not a pipe: the server pg_ctl starts keeps a pipe open and the call never returns."""
    import tempfile
    with tempfile.TemporaryFile() as output:
        try:
            subprocess.run([str(postgres_bin() / _exe(name)), *map(str, args)], check=True,
                           stdout=output, stderr=subprocess.STDOUT, timeout=timeout, **_DETACHED)
        except subprocess.CalledProcessError as error:
            output.seek(0)
            raise subprocess.CalledProcessError(error.returncode, error.cmd,
                                                output.read().decode(errors="replace")) from None
        output.seek(0)
        return output.read().decode(errors="replace")


def _server_exe(data: Path) -> str | None:
    """The program of the Postgres running on `data`, or None when none is."""
    import psutil
    try:
        pid = int((data / "postmaster.pid").read_text(encoding="utf-8").split()[0])
        process = psutil.Process(pid)
        return process.exe() if "postgres" in process.name().lower() else None
    except (OSError, ValueError, IndexError, psutil.Error):
        return None


def _current(exe: str | None) -> bool:
    return bool(exe) and (os.path.normcase(str(Path(exe).parent))
                          == os.path.normcase(str(postgres_bin())))


def _start(data: Path, port: int) -> None:
    def start():
        if not _current(_server_exe(data)):                # a failed try may have started it
            _pg_tool("pg_ctl", ["-D", data, "-w", "-t", "60", "-o", f"-p {port}",
                                "-l", data / "log", "start"], timeout=90)
    _retry(start, attempts=10)


def stop_postgres(data: Path | None = None) -> None:
    data = data or _pg_data()
    if _server_exe(data):
        _pg_tool("pg_ctl", ["-D", data, "-w", "-m", "fast", "stop"])


def _retry(step, attempts, undo=None):
    """Postgres's own tools fail at random on some Windows PCs. initdb, pg_ctl and postgres.exe
    itself (checking its own version at start) run commands through cmd.exe, and on a PC with
    AhnLab V3 running Windows refused to start cmd.exe ("Access is denied") in 10-30% of plain
    `cmd /c ver` runs while other programs always started: Postgres reports it as "popen
    failure: Bad file descriptor". There is no way around cmd.exe short of patching Postgres,
    and every attempt is independent, so trying again is the fix: ten attempts at those odds
    fail together far less than once in a million."""
    import time
    for attempt in range(attempts):
        try:
            return step()
        except subprocess.CalledProcessError:
            if undo:
                undo()
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def _initdb_with_retries(data):
    """Create the cluster, once per PC. A failed run leaves the folder half made; clear it."""
    import shutil
    _retry(lambda: _pg_tool("initdb", ["-D", data, "--auth=trust", "--encoding=UTF8",
                                       "--locale=C", "-U", "postgres"]),
           attempts=10, undo=lambda: shutil.rmtree(data, ignore_errors=True))


# Started from Claude's or Codex's process, the server shared that program's console, and closing
# it sent Ctrl+C to Postgres too (0xC000013A in its log): the hub went down with an app window.
# Its own process group, with a console of its own that is never shown, it outlives whoever
# happened to start it. Not DETACHED_PROCESS (no console at all): Postgres runs cmd.exe for its
# own checks, and each of those then opened a console window on the user's screen. Its stdin is
# the null device: Postgres must not hold on to the caller's (an MCP server's is the pipe its app
# talks through), and a caller may have none at all (pythonw).
_DETACHED = ({"creationflags": 0x08000000 | 0x00000200,     # CREATE_NO_WINDOW | NEW_PROCESS_GROUP
              "stdin": subprocess.DEVNULL}
             if os.name == "nt" else {"start_new_session": True, "stdin": subprocess.DEVNULL})


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
    opened = open_to_network(open_firewall)
    set_folder("local")
    sealed = _seal_existing(local_postgres())
    code = CODE_PREFIX + base64.urlsafe_b64encode(json.dumps(
        {"h": opened["addresses"], "p": PORT, "k": opened["password"],
         "c": opened["certificate"]}).encode()).decode().rstrip("=")
    return {"connection_code": code, "addresses": opened["addresses"], "port": PORT,
            "lines_sealed": sealed, "firewall": opened["firewall"]}


def open_to_network(open_firewall: bool = True) -> dict:
    """Make this PC's Postgres reachable from the network, over TLS and with a password only,
    without moving this PC's personal memory there: a team hub on a NAS or a server needs only
    this. The hub's certificate goes into every code, so a PC checks it reached this hub."""
    import psycopg
    import secrets
    local_postgres()                        # first: it explains a machine it cannot host on
    password = _config().get("share_password") or secrets.token_urlsafe(18)
    _save({"share_password": password})          # before sealing: the line key derives from it
    data = _pg_data()
    certificate = _certificate(data)
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        # token_urlsafe is [A-Za-z0-9_-]: nothing in it can close the quoted literal
        admin.execute(f"ALTER ROLE graphmind PASSWORD '{password}'")
        restart = admin.execute("SHOW listen_addresses").fetchone()[0] != "*"
        if restart:
            admin.execute("ALTER SYSTEM SET listen_addresses = '*'")
        if admin.execute("SHOW ssl").fetchone()[0] != "on":
            admin.execute("ALTER SYSTEM SET ssl = on")
    allow_from_network("graphmind graphmind", "other PCs, with the password")
    if restart:
        _retry(lambda: _pg_tool("pg_ctl", ["-D", data, "-w", "-m", "fast", "restart"]),
               attempts=10)
    return {"password": password, "addresses": _addresses(), "certificate": certificate,
            "firewall": _firewall() if open_firewall else "not changed"}


def allow_from_network(database_and_role: str, why: str) -> None:
    """A rule letting `database role` in from other machines: over TLS, with the password.
    The plain rules written before TLS become TLS-only. Then the server reads its settings."""
    import psycopg
    import time
    hba = _pg_data() / "pg_hba.conf"
    plain = f"host {database_and_role} all scram-sha-256"
    rule = "hostssl " + plain[len("host "):]
    before = hba.read_text(encoding="utf-8").splitlines()
    lines = [rule if line.strip() == plain else line for line in before]
    if rule not in lines:
        lines += ["", f"# Graph-MIND: {why}", rule]
    if lines != before:
        hba.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with psycopg.connect(_admin_uri(), autocommit=True) as admin:
        admin.execute("SELECT pg_reload_conf()")
    for _ in range(50):                      # the reload is a signal: wait until it took
        with psycopg.connect(_admin_uri(), autocommit=True) as admin:
            if admin.execute("SHOW ssl").fetchone()[0] == "on":
                break
        time.sleep(0.1)
    time.sleep(0.3)                          # ponytail: the rules have no "loaded" flag to read


def forbid_from_network(database_and_role: str) -> None:
    """Take back a rule allow_from_network wrote (teammates once reached the personal memory
    database's server entry; now only the hub's)."""
    import psycopg
    hba = _pg_data() / "pg_hba.conf"
    gone = {f"{kind} {database_and_role} all scram-sha-256" for kind in ("host", "hostssl")}
    before = hba.read_text(encoding="utf-8").splitlines()
    lines = [line for line in before if line.strip() not in gone]
    if lines != before:
        hba.write_text("\n".join(lines) + "\n", encoding="utf-8")
        with psycopg.connect(_admin_uri(), autocommit=True) as admin:
            admin.execute("SELECT pg_reload_conf()")


def _certificate(data: Path) -> str:
    """This hub's TLS certificate (DER, base64url), made once: self-signed, and carried by every
    code for this hub, so a PC accepts this hub and nothing in between (sslmode=verify-ca)."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    crt, key = data / "server.crt", data / "server.key"
    if not (crt.exists() and key.exists()):
        import datetime as dt
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
        private = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Graph-MIND hub")])
        now = dt.datetime.now(dt.timezone.utc)
        made = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(private.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1))
                .not_valid_after(now + dt.timedelta(days=365 * 30))
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                .sign(private, hashes.SHA256()))
        key.write_bytes(private.private_bytes(serialization.Encoding.PEM,
                                              serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
        if os.name != "nt":
            os.chmod(key, 0o600)             # Postgres refuses a key others can read
        crt.write_bytes(made.public_bytes(serialization.Encoding.PEM))
    der = x509.load_pem_x509_certificate(crt.read_bytes()).public_bytes(
        serialization.Encoding.DER)
    return base64.urlsafe_b64encode(der).decode().rstrip("=")


def tls_options(certificate: str | None) -> str:
    """The connection settings for a hub: its own certificate pinned when the code carries one.
    A code from before TLS has none; the connection is then still encrypted whenever the hub
    offers it (libpq's default), only without the check."""
    if not certificate:
        return ""
    import hashlib
    import ssl
    import urllib.parse
    from development_paths import graph_mind_home
    der = base64.urlsafe_b64decode(certificate + "=" * (-len(certificate) % 4))
    path = graph_mind_home() / "hubs" / f"{hashlib.sha256(der).hexdigest()[:16]}.crt"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="ascii")
    return "?sslmode=verify-ca&sslrootcert=" + urllib.parse.quote(str(path), safe="")


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
        return {"h": [str(h) for h in value["h"]], "p": int(value["p"]), "k": str(value["k"]),
                "c": value.get("c")}
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
           f"@{host}:{value['p']}/graphmind" + tls_options(value["c"]))
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
        return _pg_append_or_queue(shared, lines)
    log = Path(shared).expanduser() / "log" / f"{device_name()}.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write("\n".join(lines) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return len(lines)


def _shared_ids(shared) -> set:
    """(kind, id) of every turn and memory the shared log holds, forgotten ones included."""
    if is_database(shared):
        with _pg(shared) as connection:
            lines = [line for line in (_unseal(shared, row[0]) for row in
                     connection.execute("SELECT line FROM brain_log")) if line is not None]
    else:
        lines = [line for log in (Path(shared) / "log").glob("*.jsonl")
                 for line in log.read_text(encoding="utf-8", errors="replace").splitlines()]
    ids = set()
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        record = item.get("record", {})
        if item.get("kind") == "turn":
            ids.add(("turn", record.get("turn_id")))
        elif item.get("kind") == "memory":
            ids.add(("memory", record.get("memory_id")))
        elif item.get("kind") == "forget":           # never resend what was deleted
            ids |= {("turn", t) for t in record.get("turn_ids", [])}
            ids |= {("memory", m) for m in record.get("memory_ids", [])}
    return ids


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
        # what the shared log already holds, whoever wrote it: a turn published as it was
        # captured is never marked exported, so without this a re-share sent everything again,
        # and a PC whose turns missed a downed hub had no way to send just those
        done |= _shared_ids(shared)
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


SEALED = "gme1:"
_KEYS = {}


def _line_key(url):
    """The key that seals this hub's lines, or None when it has none.

    The connection to a hub is TLS (since 0.7; before, it was not encrypted at all), and each
    line is sealed besides, so the hub's disk holds ciphertext only and a hub reached by a code
    from before TLS still never carries memory in clear. Each line is sealed (Fernet: AES with an HMAC) under a key derived from the password in the connection
    code, which every PC that joined holds and nobody else does. The hub stores ciphertext too.
    A joined PC finds the password in its URL; the hub PC in its own config."""
    import urllib.parse
    password = urllib.parse.urlsplit(url).password
    password = urllib.parse.unquote(password) if password else _config().get("share_password")
    if not password:
        return None                               # a hub never shared: nothing leaves this PC
    if password not in _KEYS:
        from cryptography.fernet import Fernet
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
        raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"graph-mind brain_log",
                   info=b"line key v1").derive(password.encode())
        _KEYS[password] = Fernet(base64.urlsafe_b64encode(raw))
    return _KEYS[password]


def _seal(url, lines):
    key = _line_key(url)
    return [SEALED + key.encrypt(line.encode()).decode() if key else line for line in lines]


def _unseal(url, line):
    """The plain line, or None when it cannot be opened with this hub's key."""
    if not line.startswith(SEALED):
        return line                               # written before sealing began
    key = _line_key(url)
    if key is None:
        return None
    from cryptography.fernet import InvalidToken
    try:
        return key.decrypt(line[len(SEALED):].encode()).decode()
    except InvalidToken:
        return None


def _seal_existing(url) -> int:
    """Seal the lines the hub held before it was shared, so none crosses the network in clear."""
    if _line_key(url) is None:
        return 0
    with _pg(url) as connection:
        rows = connection.execute("SELECT seq, line FROM brain_log WHERE line NOT LIKE %s",
                                  (SEALED + "%",)).fetchall()
        with connection.cursor() as cursor:
            cursor.executemany("UPDATE brain_log SET line = %s WHERE seq = %s",
                               [(_seal(url, [line])[0], seq) for seq, line in rows])
        connection.commit()
    return len(rows)


def _pg(url):
    import psycopg
    connection = psycopg.connect(url, autocommit=False)
    connection.execute("""CREATE TABLE IF NOT EXISTS brain_log(
        seq BIGSERIAL PRIMARY KEY, device TEXT NOT NULL, line TEXT NOT NULL)""")
    connection.commit()
    return connection


_READERS: dict = {}


def _read_rows(url, query, parameters):
    """A read on one connection kept open per hub: the sync before every answer paid a fresh
    connection (password handshake included) each time, 0.2 s of a 0.9 s answer. A connection
    the hub dropped (it restarted) is replaced once."""
    import psycopg
    for attempt in (1, 2):
        connection = _READERS.get(url)
        if connection is None or connection.closed:
            connection = _READERS[url] = _pg(url)
            connection.autocommit = True
        try:
            return connection.execute(query, parameters).fetchall()
        except psycopg.OperationalError:
            _READERS.pop(url, None)
            if attempt == 2:
                raise


def _outbox() -> Path:
    from development_paths import graph_mind_home
    return graph_mind_home() / "outbox.jsonl"


def _pg_append_or_queue(url, lines) -> int:
    """Append to the hub; when it cannot be reached, keep the lines in a local outbox and send
    them with the next append that gets through. The hub was down for four days once and every
    turn of those days stayed on the PC that captured it until it was sent by hand."""
    import fasteners
    outbox = _outbox()
    outbox.parent.mkdir(parents=True, exist_ok=True)
    with fasteners.InterProcessLock(str(outbox.with_suffix(".lock"))):
        waiting = ([l for l in outbox.read_text(encoding="utf-8").splitlines() if l.strip()]
                   if outbox.exists() else [])
        try:
            _pg_append(url, waiting + lines)
        except Exception:
            with outbox.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n")
            return len(lines)                     # queued, not lost
        if waiting:
            outbox.unlink()
    return len(lines)


def _pg_append(url, lines) -> int:
    with _pg(url) as connection:
        # one writer at a time: a row can then never commit after a later sequence number has
        # been read, which would make a reader's cursor step over it for good
        connection.execute("SELECT pg_advisory_xact_lock(4242)")
        with connection.cursor() as cursor:
            cursor.executemany("INSERT INTO brain_log(device, line) VALUES (%s, %s)",
                               [(device_name(), line) for line in _seal(url, lines)])
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
        rows = _read_rows(shared, "SELECT seq, line FROM brain_log WHERE seq > %s ORDER BY seq",
                          (row[0] if row else 0,))
        if rows:
            with db:
                db.execute("INSERT OR REPLACE INTO brain_log_cursor VALUES(?,?)",
                           (source, rows[-1][0]))
        opened = [_unseal(shared, line) for _, line in rows]
        return [line for line in opened if line is not None]
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
        forgets = [item["record"] for item, _ in items if item.get("kind") == "forget"]
        items = [(item, line) for item, line in items if item.get("kind") != "forget"]
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
        if forgets:                         # after the imports: a forgotten turn may be in them
            from forget import erase
            for record in forgets:
                erase(index, record)
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
