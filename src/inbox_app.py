"""The inbox: a mailbox on this PC for what project brains ask of the person.

Received reviews, sent requests and a trash box, each filterable by project, like mail folders.
A review opens as a thread: every request and answer about that result, oldest first, then the
reply box. What changed since the last version is measured from the files; the file opens in its
own app; the person's decision is recorded from here too (the click is their word, as "승인해"
is), and a decision can be changed or an approval withdrawn later.

Deleting hides a review from this person's inbox only (the trash box restores it): the review is
the team's shared record and stays for everyone else. Read and deleted marks are kept on this PC.

It is a small web server inside the capture service, bound to 127.0.0.1 and answering only
requests that carry this install's token in a header (a page elsewhere cannot add one without
asking first, and nothing here answers that ask) and name this PC as the host (no DNS
rebinding). A notification links straight to its review.

    graph-mind-inbox            # open it (serves it here if the capture service is not running)
"""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser

DEFAULT_PORT = 54330


def port() -> int:
    from brain_log import _config
    return int(_config().get("inbox_port") or DEFAULT_PORT)


def token() -> str:
    from brain_log import _config, _save
    value = _config().get("inbox_token")
    if not value:
        value = secrets.token_urlsafe(24)
        _save({"inbox_token": value})
    return value


def url(review_id: str | None = None) -> str:
    base = f"http://127.0.0.1:{port()}/?t={token()}"
    return base + (f"#review/{review_id}" if review_id else "")


# -- read and deleted marks, on this PC only ----------------------------------------------------
_STATE_LOCK = threading.Lock()


def _state_path():
    from development_paths import graph_mind_home
    return graph_mind_home() / "inbox_state.json"


def _state() -> dict:
    try:
        value = json.loads(_state_path().read_text(encoding="utf-8"))
        return {"hidden": set(value.get("hidden", [])), "read": set(value.get("read", []))}
    except (OSError, ValueError):
        return {"hidden": set(), "read": set()}


def mark(review_id: str, *, hidden: bool | None = None, read_key: str | None = None) -> dict:
    with _STATE_LOCK:
        state = _state()
        if hidden is True:
            state["hidden"].add(review_id)
        elif hidden is False:
            state["hidden"].discard(review_id)
        if read_key:
            state["read"].add(read_key)
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps({key: sorted(value) for key, value in state.items()}),
                             encoding="utf-8")
        os.replace(temporary, path)
    return {"review_id": review_id, "hidden": review_id in state["hidden"]}


# -- what the page shows -------------------------------------------------------------------------
def inbox() -> dict:
    import project_ledger as pl
    found, unreachable = pl.ledgers()
    state = _state()
    received, sent, projects = [], [], {}
    try:
        for book in found:
            for review in book.reviews(mine=True):
                item = _brief(review, state)
                (received if review["for_me"] == "to review" else sent).append(item)
            for name in book.names():
                role = next((m["role"] for m in book.members(name) if m["member"] == book.by), "")
                projects[name] = role
    finally:
        for book in found:
            book.db.close()
    newest = lambda r: r["responded_at"] or r["requested_at"] or ""  # noqa: E731
    received.sort(key=lambda r: r["requested_at"] or "", reverse=True)
    sent.sort(key=newest, reverse=True)
    return {"received": received, "sent": sent,
            "projects": [{"name": n, "role": r} for n, r in sorted(projects.items())],
            "hosting": _hosting(), "unreachable": unreachable}


def _hosting() -> bool:
    """Whether this PC runs a hub whose operator tools work here (the database's own admin)."""
    import project_ledger as pl
    try:
        return pl._hosts_here() and bool(pl._admin().close() or True)
    except Exception:
        return False


def _brief(review: dict, state: dict) -> dict:
    artifact = review.get("artifact") or {}
    # what is new to this person: a request waiting for them, or an answer to theirs
    key = f"{review['id']}:{review.get('responded_at') or review.get('requested_at')}"
    unread = (review["status"] == "requested" if review["for_me"] == "to review"
              else bool(review.get("response")) and key not in state["read"])
    return {"id": review["id"], "project": review["project"], "status": review["status"],
            "requester": review["requester"], "reviewer": review["reviewer"],
            "name": artifact.get("name") or artifact.get("repo") or "result",
            "version": artifact.get("version"), "focus": review.get("focus"),
            "note": review.get("note"), "requested_at": review.get("requested_at"),
            "responded_at": review.get("responded_at"), "response": review.get("response"),
            "key": key, "unread": unread, "hidden": review["id"] in state["hidden"]}


def _with_review(review_id: str, work):
    import project_ledger as pl
    book = pl.ledger_with_review(review_id)
    try:
        return work(book)
    finally:
        book.db.close()


def review(review_id: str) -> dict:
    import project_ledger as pl

    def read(book):
        opened = book.open_review(review_id, into=pl.downloads(), by=book.by)
        opened["mine_to_answer"] = book._is_reviewer(opened["review"], book._review(review_id)[2])
        opened["me"], opened["hub"] = book.by, book.where
        return opened
    opened = _with_review(review_id, read)
    shown = opened["review"]
    mark(review_id, read_key=f"{review_id}:{shown.get('responded_at') or shown.get('requested_at')}")
    return opened


def open_file(review_id: str) -> dict:
    """Open the version a review is about in its own app: named by the review, never by a path
    from the page."""
    import project_ledger as pl

    def fetch(book):
        asked = book._review(review_id)[0]
        return book.fetch(asked["project"], artifact_id=asked["artifact"].get("artifact_id"),
                          into=pl.downloads(), by=book.by)
    fetched = _with_review(review_id, fetch)
    pl.open_with_default_app(fetched["path"])
    return {"opened": fetched["name"], "version": fetched["version"]}


def respond(review_id: str, decision: str, note: str | None, version, uploads=()) -> dict:
    def answer(book):
        if decision == "cancelled":
            return book.cancel_review(review_id, book.by)
        return book.respond_review(review_id, decision, book.by, note=note,
                                   version=int(version) if version else None,
                                   files=_files(uploads))
    return _with_review(review_id, answer)


def comment(review_id: str, text: str, parent_id: str | None, uploads=()) -> dict:
    return _with_review(review_id, lambda book: book.comment(
        review_id, text, book.by, parent_id=parent_id or None, files=_files(uploads)))


def edit(review_id: str, text: str, comment_id: str | None) -> dict:
    return _with_review(review_id, lambda book: book.edit_comment(comment_id, text, book.by)
                        if comment_id else book.edit_response(review_id, text, book.by))


# -- files the page sends and gets ---------------------------------------------------------------
def _uploads() -> "Path":
    from development_paths import graph_mind_home
    folder = graph_mind_home() / "uploads"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def receive(stream, length: int, name: str) -> dict:
    """A file the page sends for a comment or an answer, any size, kept here until used."""
    upload = "up-" + secrets.token_hex(8)
    folder = _uploads() / upload
    folder.mkdir()
    target = folder / (os.path.basename(name or "file")[:200] or "file")
    left = length
    with target.open("wb") as out:
        while left > 0:
            piece = stream.read(min(left, 1 << 20))
            if not piece:
                break
            out.write(piece)
            left -= len(piece)
    if left:
        raise ValueError("UPLOAD_CUT: the file did not arrive whole")
    return {"upload": upload, "name": target.name, "size": length}


def _files(uploads) -> list:
    """The received files named by the page's upload ids (never by a path from the page)."""
    files = []
    for upload in uploads or []:
        if not str(upload).startswith("up-") or not str(upload)[3:].isalnum():
            raise ValueError("UPLOAD_NOT_FOUND")
        found = list((_uploads() / upload).glob("*"))
        if not found:
            raise ValueError("UPLOAD_NOT_FOUND")
        files.append(found[0])
    return files


def attachment(review_id: str, artifact_id: str):
    import project_ledger as pl
    target = pl.downloads() / "attachments" / artifact_id
    return _with_review(review_id, lambda book: book.attachment(review_id, artifact_id, target))


# -- space ---------------------------------------------------------------------------------------
def storage(project: str) -> dict:
    import project_ledger as pl
    book = pl.ledger_for(project)
    try:
        return book.storage(project)
    finally:
        book.db.close()


def purge(project: str, name: str, keep) -> dict:
    import project_ledger as pl
    book = pl.ledger_for(project)
    try:
        return book.purge_versions(project, name, book.by, keep=int(keep or 1))
    finally:
        book.db.close()


def hub_usage() -> dict:
    import project_ledger as pl
    if not _hosting():
        raise ValueError("NOT_HOSTING: this PC does not run the hub")
    return pl.hub_usage()


def delete_project(project: str, confirm: str) -> dict:
    import project_ledger as pl
    if not _hosting():
        raise ValueError("NOT_HOSTING: this PC does not run the hub")
    return pl.delete_project(project, pl.actor(), confirm=confirm)


# -- the server ----------------------------------------------------------------------------------
class _Handler(BaseHTTPRequestHandler):
    server_version = "Graph-MIND-Inbox"

    def log_message(self, *args):               # quiet: it runs inside pythonw
        pass

    def _allowed(self, need_token: bool) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if host not in (f"127.0.0.1:{self.server.server_port}",
                        f"localhost:{self.server.server_port}"):
            return False
        if need_token:
            return secrets.compare_digest(self.headers.get("X-Graph-Mind-Token") or "",
                                          self.server.token)
        return True

    def _send(self, status: int, body: bytes, kind: str, extra=()):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for header, value in extra:
            self.send_header(header, value)
        if kind.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src blob: data:")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value, status=200):
        self._send(status, json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _answer(self, work):
        import project_ledger as pl
        try:
            self._json(work())
        except pl.HubUnavailable as error:
            self._json({"error": "UNAVAILABLE", "detail": f"{error}: the latest shared state "
                                                         "is unknown right now"}, 503)
        except ValueError as error:
            self._json({"error": "REFUSED", "detail": str(error)}, 400)
        except Exception as error:               # a hub that went away mid-call
            self._json({"error": "UNAVAILABLE", "detail": f"{type(error).__name__}: {error}"[:300]},
                       503)

    def _file(self, review_id: str, artifact_id: str):
        """An attached file, sent as it is (images shown in the page, others saved)."""
        import mimetypes
        try:
            saved = attachment(review_id, artifact_id)
        except ValueError as error:
            return self._json({"error": "REFUSED", "detail": str(error)}, 400)
        kind = mimetypes.guess_type(saved["name"])[0] or "application/octet-stream"
        if not kind.startswith("image/") or kind == "image/svg+xml":
            kind = "application/octet-stream"          # never rendered as a page
        quoted = urllib.parse.quote(saved["name"])
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(os.path.getsize(saved["path"])))
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quoted}")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with open(saved["path"], "rb") as stream:
            while True:
                piece = stream.read(1 << 20)
                if not piece:
                    break
                self.wfile.write(piece)

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        path = [urllib.parse.unquote(p) for p in parts.path.split("/")[1:]]
        if parts.path == "/":
            if not self._allowed(need_token=False):
                return self._send(403, b"forbidden", "text/plain")
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if not self._allowed(need_token=True):
            return self._json({"error": "FORBIDDEN"}, 403)
        if path == ["api", "inbox"]:
            return self._answer(inbox)
        if len(path) == 3 and path[:2] == ["api", "review"]:
            return self._answer(lambda: review(path[2]))
        if len(path) == 4 and path[:2] == ["api", "attachment"]:
            return self._file(path[2], path[3])
        if len(path) == 3 and path[:2] == ["api", "storage"]:
            return self._answer(lambda: storage(path[2]))
        if path == ["api", "hub", "usage"]:
            return self._answer(hub_usage)
        return self._json({"error": "NOT_FOUND"}, 404)

    def do_POST(self):
        if not self._allowed(need_token=True):
            return self._json({"error": "FORBIDDEN"}, 403)
        parts = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        if parts.path == "/api/upload":                # a file's raw bytes, any size
            name = urllib.parse.parse_qs(parts.query).get("name", ["file"])[0]
            return self._answer(lambda: receive(self.rfile, length, name))
        try:
            body = json.loads(self.rfile.read(min(length, 256 * 1024)) or b"{}")
        except ValueError:
            return self._json({"error": "BAD_REQUEST"}, 400)
        path, text = parts.path, body.get("text") or body.get("note")
        review_id = str(body.get("review_id") or "")
        if path == "/api/open":
            return self._answer(lambda: open_file(review_id))
        if path == "/api/mark":
            return self._answer(lambda: mark(review_id, hidden=body.get("hidden")))
        if path == "/api/respond":
            if body.get("decision") not in ("approved", "changes_requested", "withdrawn",
                                            "cancelled"):
                return self._json({"error": "BAD_REQUEST"}, 400)
            return self._answer(lambda: respond(review_id, body["decision"], text,
                                                body.get("version"), body.get("uploads")))
        if path == "/api/comment":
            return self._answer(lambda: comment(review_id, text, body.get("parent_id"),
                                                body.get("uploads")))
        if path == "/api/edit":
            return self._answer(lambda: edit(review_id, text, body.get("comment_id")))
        if path == "/api/purge":
            return self._answer(lambda: purge(str(body.get("project") or ""),
                                              str(body.get("name") or ""), body.get("keep")))
        if path == "/api/hub/delete_project":
            return self._answer(lambda: delete_project(str(body.get("project") or ""),
                                                       str(body.get("confirm") or "")))
        return self._json({"error": "NOT_FOUND"}, 404)


def serve(block: bool = False):
    """Start the inbox on this PC. Returns the server, or None when the port is taken (another
    process, usually the capture service, already serves it)."""
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port()), _Handler)
    except OSError:
        return None
    server.token = token()
    server.daemon_threads = True
    if block:
        server.serve_forever()
    else:
        threading.Thread(target=server.serve_forever, daemon=True, name="graph-mind-inbox-app").start()
    return server


def _answering() -> bool:
    import urllib.request
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{port()}/api/inbox",
                                         headers={"X-Graph-Mind-Token": token()})
        with urllib.request.urlopen(request, timeout=3):
            return True
    except Exception as error:
        return getattr(error, "code", None) in (400, 503)   # it answered, a hub did not


def main(argv=None) -> int:
    if _answering():
        webbrowser.open(url())
        return 0
    server = serve()
    if server is None:
        print(f"Port {port()} is in use by something else; set \"inbox_port\" in the Graph-MIND "
              "config.", file=sys.stderr)
        return 1
    webbrowser.open(url())
    print(f"Graph-MIND inbox: {url()}  (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    return 0


PAGE = r"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Graph-MIND 요청함</title>
<style>
:root {
  --bg: #f4f5f8; --panel: #ffffff; --ink: #1d2330; --muted: #6b7385; --line: #e3e6ec;
  --accent: #3b5bdb; --accent-ink: #ffffff; --soft: #eef2ff; --warn: #c2410c; --ok: #15803d;
  --red: #dc2626; --red-soft: #fef2f2; --chip: #eef1f6;
  --add-bg: #e8f6ec; --add-ink: #14532d; --del-bg: #fdecec; --del-ink: #7f1d1d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101318; --panel: #181c24; --ink: #e6e9ef; --muted: #98a1b3; --line: #2a313c;
    --accent: #7c95ff; --accent-ink: #0d1117; --soft: #1f2640; --warn: #fb923c; --ok: #4ade80;
    --red: #f87171; --red-soft: #2a1515; --chip: #232a35;
    --add-bg: #12301f; --add-ink: #bbf7d0; --del-bg: #3a1717; --del-ink: #fecaca;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); display: flex; flex-direction: column;
  font: 14px/1.55 "Pretendard", "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
header { background: var(--panel); border-bottom: 1px solid var(--line); }
.top { display: flex; align-items: center; gap: 12px; padding: 12px 20px 0; }
.top h1 { font-size: 15px; margin: 0; font-weight: 700; }
.top .status { margin-left: auto; color: var(--muted); font-size: 12px; }
nav { display: flex; align-items: flex-end; gap: 4px; padding: 8px 16px 0; }
nav button { border: 0; background: transparent; color: var(--muted); padding: 8px 14px 10px; font: inherit;
  cursor: pointer; border-bottom: 2px solid transparent; }
nav button.on { color: var(--ink); font-weight: 600; border-bottom-color: var(--accent); }
nav .badge { display: inline-block; min-width: 18px; padding: 0 6px; margin-left: 6px; border-radius: 9px;
  background: var(--accent); color: var(--accent-ink); font-size: 11px; line-height: 18px; text-align: center; }
nav select { margin-left: auto; margin-bottom: 8px; padding: 5px 10px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--ink); font: inherit; }
.app { flex: 1; display: grid; grid-template-columns: minmax(300px, 400px) 1fr; min-height: 0; }
.app.wide { grid-template-columns: 1fr; }
#list { border-right: 1px solid var(--line); overflow-y: auto; background: var(--panel); }
.mail { padding: 12px 16px; border-bottom: 1px solid var(--line); cursor: pointer; }
.mail:hover { background: var(--chip); }
.mail.on { background: var(--soft); }
.mail .line1 { display: flex; gap: 8px; align-items: baseline; }
.mail .from { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.mail .date { margin-left: auto; color: var(--muted); font-size: 12px; white-space: nowrap; }
.mail .subject { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; margin-top: 2px; }
.mail .preview { color: var(--muted); font-size: 12.5px; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; margin-top: 2px; }
.mail.unread .from, .mail.unread .subject { font-weight: 700; }
.mail.unread { box-shadow: inset 3px 0 0 var(--accent); }
.chip { display: inline-block; padding: 1px 8px; border-radius: 10px; background: var(--chip);
  font-size: 11px; color: var(--muted); white-space: nowrap; font-weight: 500; }
.chip.requested { color: var(--warn); }
.chip.in_review { color: var(--accent); }
.chip.approved { color: var(--ok); }
.chip.changes_requested, .chip.withdrawn { color: var(--warn); }
#read { overflow-y: auto; padding: 24px 32px 64px; }
.empty { color: var(--muted); padding: 40px 16px; text-align: center; }
h2 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.02em; }
h3 { font-size: 13px; color: var(--muted); margin: 26px 0 8px; font-weight: 600; }
.sub { color: var(--muted); margin-bottom: 12px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.request { border-left: 3px solid var(--accent); }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 14px 0; }
.row.tight { margin: 8px 0 0; }
button.act { border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  padding: 7px 14px; border-radius: 8px; font: inherit; cursor: pointer; }
button.act:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
button.warn { color: var(--warn); }
button.red { color: var(--red); border-color: var(--red); background: var(--red-soft); }
button.red:hover { border-color: var(--red); }
button.link { border: 0; background: none; color: var(--muted); font: inherit; font-size: 12px; cursor: pointer; padding: 0 6px 0 0; }
button.link:hover { color: var(--accent); }
.spacer { flex: 1; }
textarea { width: 100%; min-height: 84px; padding: 10px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--ink); font: inherit; resize: vertical; }
.meta { color: var(--muted); font-size: 12.5px; }
.replying { font-size: 12.5px; color: var(--accent); margin-bottom: 6px; }
.files { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
.file { display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px; border-radius: 8px; background: var(--chip);
  font-size: 12.5px; cursor: pointer; max-width: 280px; }
.file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.thumb { max-width: 240px; max-height: 180px; border-radius: 8px; border: 1px solid var(--line); cursor: zoom-in; display: block; }
.feed { display: grid; gap: 10px; margin-top: 14px; }
.entry { display: grid; grid-template-columns: 32px 1fr; gap: 10px; background: var(--panel);
  border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; }
.entry.reply { margin-left: 42px; }
.avatar { width: 32px; height: 32px; border-radius: 50%; background: var(--soft); color: var(--accent);
  display: grid; place-items: center; font-weight: 700; font-size: 13px; }
.who { font-size: 13px; }
.who b { font-weight: 600; margin-right: 6px; }
.who .meta { margin-left: 4px; }
.text { white-space: pre-wrap; word-break: break-word; margin-top: 2px; }
.history { margin-top: 6px; padding: 8px 10px; border-radius: 8px; background: var(--chip); font-size: 12.5px; }
.diff { font: 12.5px/1.6 ui-monospace, "Cascadia Mono", Consolas, monospace; overflow-x: auto;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
.diff div { padding: 0 12px; white-space: pre; }
.diff .add { background: var(--add-bg); color: var(--add-ink); }
.diff .del { background: var(--del-bg); color: var(--del-ink); }
.diff .hunk { color: var(--muted); background: var(--chip); }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 10px; border-bottom: 1px solid var(--line); vertical-align: top; }
th { color: var(--muted); font-weight: 600; font-size: 12.5px; }
td.num, th.num { text-align: right; white-space: nowrap; }
.bar { height: 10px; border-radius: 6px; background: var(--chip); overflow: hidden; margin: 8px 0 4px; }
.bar div { height: 100%; background: var(--accent); }
.space { max-width: 980px; }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--ink);
  color: var(--bg); padding: 10px 16px; border-radius: 10px; opacity: 0; transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.error { color: var(--warn); }
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  #list { border-right: 0; border-bottom: 1px solid var(--line); max-height: 40vh; }
  #read { padding: 16px; }
  nav { flex-wrap: wrap; }
}
</style>
</head>
<body>
<header>
  <div class="top"><h1>Graph-MIND 요청함</h1><span class="status" id="status"></span></div>
  <nav id="tabs"></nav>
</header>
<div class="app" id="app">
  <section id="list"></section>
  <section id="read"><div class="empty">메일을 고르세요.</div></section>
</div>
<div class="toast" id="toast"></div>
<script>
"use strict";
const params = new URLSearchParams(location.search);
let token = params.get("t") || "";
try {
  if (token) sessionStorage.setItem("gm-token", token); else token = sessionStorage.getItem("gm-token") || "";
} catch (e) {}
if (params.has("t")) history.replaceState(null, "", "/" + location.hash);

const STATUS = { requested: "새 요청", in_review: "검토 중", approved: "승인", changes_requested: "수정 요청",
  cancelled: "취소됨", withdrawn: "승인 취소" };
const TABS = { received: "받은 검토함", sent: "보낸 요청함", trash: "삭제함", space: "용량 관리" };
let state = { tab: "received", project: "", data: null,
  selected: (location.hash.match(/^#review\/(.+)$/) || [])[1] || "" };

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function fill(node, ...children) {
  node.replaceChildren(...children.flat(Infinity).filter(c => c !== null && c !== undefined && c !== false));
}
function recorded(text) { return text && text !== "not recorded" ? text : ""; }
function when(text, full) {
  if (!text) return "";
  const date = new Date(text);
  if (isNaN(date)) return text;
  const today = new Date().toDateString() === date.toDateString();
  return date.toLocaleString("ko-KR", full ? { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : today ? { hour: "2-digit", minute: "2-digit" } : { month: "numeric", day: "numeric" });
}
function bytes(n) {
  n = Number(n || 0);
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return (i ? n.toFixed(n < 10 ? 1 : 0) : n) + " " + units[i];
}
function flash(text) {
  const box = document.getElementById("toast");
  box.textContent = text; box.classList.add("show");
  setTimeout(() => box.classList.remove("show"), 2600);
}
async function api(path, body) {
  const response = await fetch(path, body ? { method: "POST", headers: { "X-Graph-Mind-Token": token,
    "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { headers: { "X-Graph-Mind-Token": token } });
  const data = await response.json().catch(() => ({ error: "BAD_RESPONSE" }));
  if (!response.ok) throw new Error(data.detail || data.error || response.status);
  return data;
}
async function upload(file) {
  const response = await fetch("/api/upload?name=" + encodeURIComponent(file.name),
    { method: "POST", headers: { "X-Graph-Mind-Token": token }, body: file });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || data.error);
  return data.upload;
}
async function blobOf(reviewId, item) {
  const response = await fetch(`/api/attachment/${encodeURIComponent(reviewId)}/${encodeURIComponent(item.artifact_id)}`,
    { headers: { "X-Graph-Mind-Token": token } });
  if (!response.ok) throw new Error("파일을 받지 못했습니다");
  return URL.createObjectURL(await response.blob());
}
function decisionOf(item) {
  return item.response && item.response.decision === "withdrawn" ? "withdrawn" : item.status;
}
function initial(name) { return String(name || "?").trim().slice(0, 1).toUpperCase(); }

function mails(tab, project) {
  if (!state.data) return [];
  const all = tab === "trash" ? [...state.data.received, ...state.data.sent].filter(m => m.hidden)
    : (state.data[tab] || []).filter(m => !m.hidden);
  return project ? all.filter(m => m.project === project) : all;
}
function owned() { return state.data ? state.data.projects.filter(p => p.role === "owner") : []; }

async function refresh() {
  try {
    state.data = await api("/api/inbox");
    const waiting = mails("received", "").filter(m => m.unread).length;
    document.title = (waiting ? `(${waiting}) ` : "") + "Graph-MIND 요청함";
    document.getElementById("status").textContent = state.data.unreachable.length
      ? "연결 안 된 허브: " + state.data.unreachable.join(", ") + " · 그 허브의 최신 상태는 알 수 없음"
      : "최신 · " + new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
    renderTabs();
    if (state.tab !== "space") renderList();
  } catch (error) {
    document.getElementById("status").textContent = token ? "불러오지 못함: " + error.message
      : "접근 토큰이 없습니다. graph-mind-inbox로 다시 여세요.";
  }
}

function renderTabs() {
  const tabs = ["received", "sent", "trash"];
  if (state.data && (state.data.hosting || owned().length)) tabs.push("space");
  const unread = tab => mails(tab, state.project).filter(m => m.unread).length;
  const select = el("select", { onchange: e => { state.project = e.target.value; refresh(); if (state.tab === "space") showSpace(); } },
    el("option", { value: "" }, "모든 프로젝트"),
    (state.data ? state.data.projects : []).map(p => el("option", { value: p.name, selected: state.project === p.name }, p.name)));
  fill(document.getElementById("tabs"),
    tabs.map(tab => el("button", { class: state.tab === tab ? "on" : "", onclick: () => switchTab(tab) }, TABS[tab],
      (tab === "received" || tab === "sent") && unread(tab) ? el("span", { class: "badge" }, unread(tab)) : null)),
    select);
}
function switchTab(tab) {
  state.tab = tab;
  document.getElementById("app").classList.toggle("wide", tab === "space");
  renderTabs();
  if (tab === "space") { document.getElementById("list").hidden = true; showSpace(); }
  else { document.getElementById("list").hidden = false; renderList();
    if (!state.selected) fill(document.getElementById("read"), el("div", { class: "empty" }, "메일을 고르세요.")); }
}

function renderList() {
  const list = document.getElementById("list");
  const shown = mails(state.tab, state.project);
  if (!shown.length) {
    fill(list, el("div", { class: "empty" }, { received: "받은 검토 요청이 없습니다.",
      sent: "보낸 요청이 없습니다.", trash: "삭제함이 비어 있습니다." }[state.tab]));
    return;
  }
  fill(list, shown.map(m => {
    const mine = state.data.sent.some(s => s.id === m.id);
    const status = decisionOf(m);
    return el("div", { class: "mail" + (m.unread && state.tab !== "trash" ? " unread" : "") + (state.selected === m.id ? " on" : ""),
      onclick: () => select(m.id) },
      el("div", { class: "line1" }, el("span", { class: "from" }, mine ? "받는 사람: " + m.reviewer : m.requester),
        el("span", { class: "chip " + status }, STATUS[status] || status),
        el("span", { class: "date" }, when(m.responded_at || m.requested_at))),
      el("div", { class: "subject" }, `${m.name}${m.version ? " v" + m.version : ""} 확인 요청`),
      el("div", { class: "preview" }, el("span", { class: "chip" }, m.project), " ",
        mine && m.response ? (m.response.note || STATUS[status]) : recorded(m.focus) || recorded(m.note)));
  }));
}

function select(id) {
  state.selected = id;
  history.replaceState(null, "", "/#review/" + id);
  renderList();
  showReview(id);
}

function attachmentsView(reviewId, items) {
  if (!items || !items.length) return null;
  return el("div", { class: "files" }, items.map(item => {
    const image = /\.(png|jpe?g|gif|webp|bmp)$/i.test(item.name);
    if (image) {
      const img = el("img", { class: "thumb", alt: item.name, title: item.name });
      blobOf(reviewId, item).then(url => { img.src = url; img.onclick = () => window.open(url); }).catch(() => {});
      return img;
    }
    return el("div", { class: "file", title: "받기", onclick: async () => {
      try { const url = await blobOf(reviewId, item); const a = el("a", { href: url, download: item.name }); a.click(); }
      catch (error) { flash(error.message); } } }, "📄", el("span", {}, item.name), el("span", { class: "meta" }, bytes(item.size)));
  }));
}

async function showReview(id) {
  const read = document.getElementById("read");
  fill(read, el("div", { class: "empty" }, "불러오는 중…"));
  let data;
  try { data = await api("/api/review/" + encodeURIComponent(id)); }
  catch (error) { fill(read, el("div", { class: "empty error" }, error.message)); return; }
  const review = data.review, artifact = review.artifact || {};
  const name = artifact.name || artifact.repo || "결과물";
  const label = `${name}${artifact.version ? " v" + artifact.version : ""}`;
  const status = decisionOf({ ...review });
  const hidden = state.data && [...state.data.received, ...state.data.sent].some(m => m.id === id && m.hidden);
  const ask = `${review.project} 프로젝트의 검토 요청 ${id}(${label}) 열어서 확인할 것 기준으로 검토해줘.`;
  const summary = recorded(data.context.publishers_account.what);
  const changes = data.changes || {};
  const diff = changes.changes && changes.changes.diff;
  const open = review.status === "requested" || review.status === "in_review";

  // the reply box: a comment, a reply to one, and for the reviewer a decision too
  let replyTo = null, chosen = [];
  const box = el("textarea", { placeholder: "댓글이나 답장을 적으세요" });
  const replying = el("div", { class: "replying", hidden: true });
  const picked = el("div", { class: "files" });
  const picker = el("input", { type: "file", multiple: true, hidden: true, onchange: e => {
    chosen.push(...e.target.files); e.target.value = ""; showChosen(); } });
  const showChosen = () => fill(picked, chosen.map((file, i) => el("div", { class: "file" }, "📎",
    el("span", {}, file.name), el("span", { class: "meta" }, bytes(file.size)),
    el("button", { class: "link", onclick: () => { chosen.splice(i, 1); showChosen(); } }, "✕"))));
  const setReply = entry => {
    replyTo = entry ? entry.id : null;
    fill(replying, entry ? [`↳ ${entry.by}님에게 답글 `, el("button", { class: "link", onclick: () => setReply(null) }, "취소")] : []);
    replying.hidden = !entry;
    if (entry) box.focus();
  };
  const send = async (kind) => {
    const text = box.value.trim();
    if (kind === "changes_requested" && !text) { flash("수정할 내용을 적어 주세요."); box.focus(); return; }
    if (kind === "comment" && !text && !chosen.length) { flash("내용을 적거나 파일을 첨부하세요."); return; }
    try {
      const uploads = [];
      for (const file of chosen) uploads.push(await upload(file));
      if (kind === "comment") await api("/api/comment", { review_id: id, text, parent_id: replyTo, uploads });
      else await api("/api/respond", { review_id: id, decision: kind, note: text, version: artifact.version, uploads });
      flash({ comment: "댓글을 남겼습니다.", approved: `${label} 승인을 보냈습니다.`, changes_requested: "수정 요청을 보냈습니다.",
        withdrawn: "승인을 취소했습니다.", cancelled: "요청을 취소했습니다." }[kind]);
      await refresh(); showReview(id);
    } catch (error) { flash(error.message); }
  };

  // everything said about this result, newest first: decisions, and comments with their replies
  const responses = r => [...(r.earlier_responses || []), ...(r.response ? [{ ...r.response, at: r.responded_at, latest: true }] : [])];
  const others = (data.history.reviews_of_this_result || []).filter(r => r.id !== id);
  const decisions = [...others.flatMap(r => responses(r).map(a => ({ ...a, version: r.version, other: true }))),
    ...responses(review).map(a => ({ ...a, version: artifact.version }))];
  const comments = data.comments || [];
  const top = comments.filter(c => !c.parent_id);
  const repliesOf = c => comments.filter(x => x.parent_id === c.id);
  const items = [...decisions.map(d => ({ kind: "decision", at: d.at, d })), ...top.map(c => ({ kind: "comment", at: c.at, c }))]
    .sort((a, b) => String(b.at).localeCompare(String(a.at)));

  const editor = (entryText, save) => {
    const area = el("textarea", {}, entryText || "");
    const holder = el("div", {}, area, el("div", { class: "row tight" },
      el("button", { class: "act primary", onclick: async () => { try { await save(area.value); flash("수정했습니다. 이전 내용은 기록에 남습니다.");
        showReview(id); } catch (error) { flash(error.message); } } }, "저장"),
      el("button", { class: "act", onclick: () => showReview(id) }, "취소")));
    return holder;
  };
  const historyView = edits => edits && edits.length ? el("details", {},
    el("summary", { class: "meta" }, `편집됨 · 이전 내용 ${edits.length}개`),
    el("div", { class: "history" }, edits.map(e => el("div", {}, el("span", { class: "meta" }, when(e.at, true) + " "), e.text ?? e.note)))) : null;
  const commentEntry = (c, reply) => {
    const body = el("div", {});
    const mine = c.by === data.me;
    fill(body,
      el("div", { class: "who" }, el("b", {}, c.by), el("span", { class: "meta" }, when(c.at, true))),
      c.text ? el("div", { class: "text" }, c.text) : null,
      attachmentsView(id, c.attachments), historyView(c.edits),
      el("div", { class: "row tight" },
        !reply ? el("button", { class: "link", onclick: () => setReply(c) }, "답글") : null,
        mine ? el("button", { class: "link", onclick: () => fill(body, el("div", { class: "who" }, el("b", {}, c.by)),
          editor(c.text, text => api("/api/edit", { review_id: id, comment_id: c.id, text }))) }, "편집") : null));
    return el("div", { class: "entry" + (reply ? " reply" : "") }, el("div", { class: "avatar" }, initial(c.by)), body);
  };
  const decisionEntry = d => {
    const body = el("div", {});
    const editable = !d.other && d.latest && d.by === data.me;
    fill(body,
      el("div", { class: "who" }, el("b", {}, d.by || review.reviewer),
        el("span", { class: "chip " + d.decision }, STATUS[d.decision] || d.decision), " ",
        el("span", { class: "meta" }, `${d.version ? "v" + d.version + " · " : ""}${when(d.at, true)}`)),
      d.note ? el("div", { class: "text" }, d.note) : null,
      attachmentsView(d.other ? null : id, d.other ? [] : d.attachments), historyView(d.edits),
      editable ? el("div", { class: "row tight" }, el("button", { class: "link", onclick: () => fill(body,
        el("div", { class: "who" }, el("b", {}, d.by)),
        editor(d.note, text => api("/api/edit", { review_id: id, text }))) }, "편집")) : null);
    return el("div", { class: "entry" }, el("div", { class: "avatar" }, initial(d.by || review.reviewer)), body);
  };

  fill(read,
    el("div", { class: "sub" }, el("span", { class: "chip" }, review.project), " ",
      el("span", { class: "chip " + status }, STATUS[status] || status)),
    el("h2", {}, `${label} 확인 요청`),
    el("div", { class: "row" },
      artifact.artifact_id ? el("button", { class: "act", onclick: async () => {
        try { const r = await api("/api/open", { review_id: id }); flash(`${r.opened} v${r.version}을(를) 열었습니다.`); }
        catch (error) { flash(error.message); } } }, "파일 열기") : null,
      el("button", { class: "act", title: ask, onclick: async () => {
        try { await navigator.clipboard.writeText(ask); flash("복사했습니다. Claude나 Codex에 붙여넣으세요."); }
        catch (e) { prompt("AI에게 붙여넣으세요", ask); } } }, "AI에게 맡기기"),
      !data.mine_to_answer && open ? el("button", { class: "act warn", onclick: () => send("cancelled") }, "요청 취소") : null,
      el("span", { class: "spacer" }),
      el("button", { class: hidden ? "act" : "act red", onclick: async () => {
        try { await api("/api/mark", { review_id: id, hidden: !hidden });
          flash(hidden ? "복원했습니다." : "삭제함으로 옮겼습니다. 상대방 기록은 그대로입니다.");
          await refresh(); showReview(id); } catch (error) { flash(error.message); } } }, hidden ? "복원" : "삭제")),
    el("div", { class: "card request" },
      el("div", { class: "meta" }, `${review.requester} → ${review.reviewer} · ${when(review.requested_at, true)}`),
      recorded(review.focus) ? el("div", { class: "text", style: "margin-top:6px" }, review.focus) : null,
      review.note ? el("div", { class: "meta", style: "margin-top:4px" }, review.note) : null,
      summary ? el("div", { class: "meta", style: "margin-top:4px" }, "바꾼 내용: " + summary) : null),
    diff ? el("h3", {}, `바뀐 점 (v${changes.from} → v${changes.to})`) : null,
    diff ? el("div", { class: "diff" }, diff.map(line => el("div", { class:
      line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "" }, line))) : null,
    el("h3", {}, "답장"),
    el("div", { class: "card" }, replying, box, picker, picked,
      el("div", { class: "row tight" },
        el("button", { class: "act", onclick: () => picker.click() }, "📎 파일 첨부"),
        el("span", { class: "spacer" }),
        el("button", { class: "act", onclick: () => send("comment") }, "댓글 달기"),
        data.mine_to_answer && review.status !== "cancelled" ? [
          review.status === "approved" ? el("button", { class: "act", onclick: () => send("withdrawn") }, "승인 취소")
            : el("button", { class: "act primary", onclick: () => send("approved") }, "승인"),
          el("button", { class: "act warn", onclick: () => send("changes_requested") }, "수정 요청")] : null)),
    el("div", { class: "feed" }, items.map(item => item.kind === "decision" ? decisionEntry(item.d)
      : [commentEntry(item.c, false), repliesOf(item.c).map(r => commentEntry(r, true))])));
}

async function showSpace() {
  const read = document.getElementById("read");
  fill(read, el("div", { class: "empty" }, "불러오는 중…"));
  const parts = [];
  if (state.data.hosting) {
    try {
      const hub = await api("/api/hub/usage");
      const used = hub.disk.total - hub.disk.free;
      parts.push(el("h2", {}, "허브 용량"),
        el("div", { class: "sub" }, `이 PC가 운영하는 허브 · 파일 위치 ${hub.folder}`),
        el("div", { class: "card" },
          el("div", {}, `디스크 ${bytes(used)} 사용 / ${bytes(hub.disk.total)} · 남은 공간 ${bytes(hub.disk.free)} · 프로젝트 파일 ${bytes(hub.stored_bytes)}`),
          el("div", { class: "bar" }, el("div", { style: `width:${Math.min(100, used / hub.disk.total * 100).toFixed(1)}%` })),
          el("div", { class: "meta" }, "파일 이름과 내용은 프로젝트 키로 잠겨 있어 운영자에게 보이지 않습니다. 크기와 개수만 표시합니다.")),
        el("h3", {}, "프로젝트별 사용량"),
        el("div", { class: "card" }, hub.projects.length ? el("table", {},
          el("tr", {}, el("th", {}, "프로젝트"), el("th", {}, "Owner"), el("th", { class: "num" }, "파일"),
            el("th", { class: "num" }, "용량"), el("th", {}, "마지막 업로드"), el("th", {}, "")),
          hub.projects.filter(p => !state.project || p.project === state.project).map(p => el("tr", {},
            el("td", {}, p.project), el("td", {}, p.owners.join(", ")), el("td", { class: "num" }, p.files),
            el("td", { class: "num" }, bytes(p.bytes)), el("td", {}, when(p.last_upload, true) || "-"),
            el("td", { class: "num" }, el("button", { class: "act red", onclick: async () => {
              const typed = prompt(`'${p.project}' 프로젝트를 통째로 삭제합니다. 되돌릴 수 없습니다.\n삭제하려면 프로젝트 이름을 그대로 입력하세요.`);
              if (typed === null) return;
              try { const r = await api("/api/hub/delete_project", { project: p.project, confirm: typed });
                flash(`${r.deleted} 삭제 · ${bytes(r.freed_bytes)} 확보`); await refresh(); showSpace(); }
              catch (error) { flash(error.message); } } }, "삭제")))))
          : el("div", { class: "meta" }, "아직 프로젝트가 없습니다.")));
    } catch (error) { parts.push(el("div", { class: "error" }, error.message)); }
  }
  for (const project of owned().filter(p => !state.project || p.name === state.project)) {
    try {
      const used = await api("/api/storage/" + encodeURIComponent(project.name));
      parts.push(el("h3", {}, `${project.name} · ${bytes(used.total)} (내가 Owner)`),
        el("div", { class: "card" }, used.files.length ? el("table", {},
          el("tr", {}, el("th", {}, "파일"), el("th", {}, "버전"), el("th", { class: "num" }, "용량"), el("th", {}, "")),
          used.files.map(f => el("tr", {},
            el("td", {}, f.name),
            el("td", {}, f.versions.map(v => `v${v.version}${v.stored ? "" : " (정리됨)"}`).join(" · ")),
            el("td", { class: "num" }, bytes(f.size)),
            el("td", { class: "num" }, f.versions.filter(v => v.stored).length > 1 ? el("button", { class: "act", onclick: async () => {
              if (!confirm(`${f.name}의 최신 버전만 남기고 이전 버전 파일을 지웁니다. 기록은 남습니다.`)) return;
              try { const r = await api("/api/purge", { project: project.name, name: f.name, keep: 1 });
                flash(`${bytes(r.freed_bytes)} 확보`); showSpace(); } catch (error) { flash(error.message); } } }, "이전 버전 정리") : null))),
          used.attachments ? el("tr", {}, el("td", {}, "검토 대화의 첨부 파일"), el("td", {}), el("td", { class: "num" }, bytes(used.attachments)), el("td", {})) : null)
          : el("div", { class: "meta" }, "저장된 파일이 없습니다.")));
    } catch (error) { parts.push(el("div", { class: "error" }, `${project.name}: ${error.message}`)); }
  }
  fill(read, el("div", { class: "space" }, parts.length ? parts : el("div", { class: "empty" }, "볼 수 있는 용량 정보가 없습니다.")));
}

(async () => {
  await refresh();
  if (state.selected) {
    if (state.data && state.data.sent.some(m => m.id === state.selected)) state.tab = "sent";
    renderTabs();
    select(state.selected);
  }
  setInterval(refresh, 10000);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    sys.exit(main())
