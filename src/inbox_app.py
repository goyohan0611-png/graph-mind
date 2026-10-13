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
    received, sent, projects = [], [], set()
    try:
        for book in found:
            for review in book.reviews(mine=True):
                item = _brief(review, state)
                (received if review["for_me"] == "to review" else sent).append(item)
            projects.update(book.names())
    finally:
        for book in found:
            book.db.close()
    newest = lambda r: r["responded_at"] or r["requested_at"] or ""  # noqa: E731
    received.sort(key=lambda r: r["requested_at"] or "", reverse=True)
    sent.sort(key=newest, reverse=True)
    return {"received": received, "sent": sent, "projects": sorted(projects),
            "unreachable": unreachable}


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


def review(review_id: str) -> dict:
    import project_ledger as pl
    book = pl.ledger_with_review(review_id)
    try:
        opened = book.open_review(review_id, into=pl.downloads(), by=book.by)
        opened["mine_to_answer"] = book._is_reviewer(opened["review"], book._review(review_id)[2])
        opened["hub"] = book.where
    finally:
        book.db.close()
    shown = opened["review"]
    mark(review_id, read_key=f"{review_id}:{shown.get('responded_at') or shown.get('requested_at')}")
    return opened


def open_file(review_id: str) -> dict:
    """Open the version a review is about in its own app: named by the review, never by a path
    from the page."""
    import project_ledger as pl
    book = pl.ledger_with_review(review_id)
    try:
        asked = book._review(review_id)[0]
        fetched = book.fetch(asked["project"], artifact_id=asked["artifact"].get("artifact_id"),
                             into=pl.downloads(), by=book.by)
    finally:
        book.db.close()
    pl.open_with_default_app(fetched["path"])
    return {"opened": fetched["name"], "version": fetched["version"]}


def respond(review_id: str, decision: str, note: str | None, version) -> dict:
    import project_ledger as pl
    book = pl.ledger_with_review(review_id)
    try:
        if decision == "cancelled":
            return book.cancel_review(review_id, book.by)
        return book.respond_review(review_id, decision, book.by, note=note,
                                   version=int(version) if version else None)
    finally:
        book.db.close()


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

    def _send(self, status: int, body: bytes, kind: str):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        if kind.startswith("text/html"):
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; script-src 'unsafe-inline'; "
                             "style-src 'unsafe-inline'; connect-src 'self'; img-src data:")
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

    def do_GET(self):
        parts = urllib.parse.urlsplit(self.path)
        if parts.path == "/":
            if not self._allowed(need_token=False):
                return self._send(403, b"forbidden", "text/plain")
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if not self._allowed(need_token=True):
            return self._json({"error": "FORBIDDEN"}, 403)
        if parts.path == "/api/inbox":
            return self._answer(inbox)
        if parts.path.startswith("/api/review/"):
            return self._answer(lambda: review(urllib.parse.unquote(parts.path[12:])))
        return self._json({"error": "NOT_FOUND"}, 404)

    def do_POST(self):
        if not self._allowed(need_token=True):
            return self._json({"error": "FORBIDDEN"}, 403)
        length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self._json({"error": "BAD_REQUEST"}, 400)
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/open":
            return self._answer(lambda: open_file(str(body.get("review_id") or "")))
        if path == "/api/mark":
            return self._answer(lambda: mark(str(body.get("review_id") or ""),
                                             hidden=body.get("hidden")))
        if path == "/api/respond":
            if body.get("decision") not in ("approved", "changes_requested", "withdrawn",
                                            "cancelled"):
                return self._json({"error": "BAD_REQUEST"}, 400)
            return self._answer(lambda: respond(body.get("review_id") or "", body["decision"],
                                                body.get("note"), body.get("version")))
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
  --chip: #eef1f6; --add-bg: #e8f6ec; --add-ink: #14532d; --del-bg: #fdecec; --del-ink: #7f1d1d;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #101318; --panel: #181c24; --ink: #e6e9ef; --muted: #98a1b3; --line: #2a313c;
    --accent: #7c95ff; --accent-ink: #0d1117; --soft: #1f2640; --warn: #fb923c; --ok: #4ade80;
    --chip: #232a35; --add-bg: #12301f; --add-ink: #bbf7d0; --del-bg: #3a1717; --del-ink: #fecaca;
  }
}
* { box-sizing: border-box; }
html, body { height: 100%; }
body { margin: 0; background: var(--bg); color: var(--ink); display: flex; flex-direction: column;
  font: 14px/1.55 "Pretendard", "Segoe UI", "Apple SD Gothic Neo", "Malgun Gothic", sans-serif; }
header { display: flex; align-items: center; gap: 12px; padding: 12px 20px; background: var(--panel);
  border-bottom: 1px solid var(--line); }
header h1 { font-size: 15px; margin: 0; font-weight: 700; }
header .status { margin-left: auto; color: var(--muted); font-size: 12px; }
.app { flex: 1; display: grid; grid-template-columns: 220px minmax(300px, 400px) 1fr; min-height: 0; }
aside { background: var(--panel); border-right: 1px solid var(--line); padding: 12px 8px; overflow-y: auto; }
.box { display: flex; align-items: center; gap: 8px; width: 100%; border: 0; background: transparent;
  color: var(--ink); padding: 8px 12px; border-radius: 8px; font: inherit; cursor: pointer; text-align: left; }
.box:hover { background: var(--chip); }
.box.on { background: var(--soft); color: var(--accent); font-weight: 600; }
.box .n { margin-left: auto; font-size: 12px; color: var(--muted); }
.box.on .n, .box .n.new { color: var(--accent); font-weight: 700; }
.label { color: var(--muted); font-size: 11px; font-weight: 600; padding: 16px 12px 6px; letter-spacing: .02em; }
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
.chip.project { color: var(--muted); }
#read { overflow-y: auto; padding: 24px 32px 48px; }
.empty { color: var(--muted); padding: 40px 16px; text-align: center; }
h2 { font-size: 20px; margin: 0 0 4px; letter-spacing: -0.02em; }
h3 { font-size: 13px; color: var(--muted); margin: 26px 0 8px; font-weight: 600; }
.sub { color: var(--muted); margin-bottom: 14px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin: 14px 0; }
button.act { border: 1px solid var(--line); background: var(--panel); color: var(--ink);
  padding: 7px 14px; border-radius: 8px; font: inherit; cursor: pointer; }
button.act:hover { border-color: var(--accent); }
button.primary { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); font-weight: 600; }
button.danger { color: var(--warn); }
button.quiet { color: var(--muted); }
textarea { width: 100%; min-height: 80px; margin-top: 10px; padding: 10px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); font: inherit; resize: vertical; }
.meta { color: var(--muted); font-size: 12.5px; }
.thread { display: grid; gap: 10px; }
.msg { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; }
.msg.now { border-left: 3px solid var(--accent); }
.msg.reply { margin-left: 28px; background: var(--chip); }
.msg .who { color: var(--muted); font-size: 12px; margin-bottom: 4px; }
.msg .title { font-weight: 600; margin-bottom: 2px; }
.diff { font: 12.5px/1.6 ui-monospace, "Cascadia Mono", Consolas, monospace; overflow-x: auto;
  border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
.diff div { padding: 0 12px; white-space: pre; }
.diff .add { background: var(--add-bg); color: var(--add-ink); }
.diff .del { background: var(--del-bg); color: var(--del-ink); }
.diff .hunk { color: var(--muted); background: var(--chip); }
.toast { position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%); background: var(--ink);
  color: var(--bg); padding: 10px 16px; border-radius: 10px; opacity: 0; transition: opacity .2s; pointer-events: none; }
.toast.show { opacity: 1; }
.error { color: var(--warn); }
@media (max-width: 900px) {
  .app { grid-template-columns: 1fr; }
  aside { display: flex; flex-wrap: wrap; gap: 4px; border-right: 0; border-bottom: 1px solid var(--line); }
  aside .box { width: auto; }
  aside .label { display: none; }
  #list { border-right: 0; border-bottom: 1px solid var(--line); max-height: 40vh; }
  #read { padding: 16px; }
}
</style>
</head>
<body>
<header>
  <h1>Graph-MIND 요청함</h1>
  <span class="status" id="status"></span>
</header>
<div class="app">
  <aside id="boxes"></aside>
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
const BOXES = { received: "받은 검토함", sent: "보낸 요청함", trash: "삭제함" };
let state = { box: "received", project: "", data: null, selected: (location.hash.match(/^#review\/(.+)$/) || [])[1] || "" };

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
function fill(node, ...children) {              // replaceChildren would print "null"
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
function decisionOf(item) {
  return item.response && item.response.decision === "withdrawn" ? "withdrawn" : item.status;
}

function mails(box, project) {
  if (!state.data) return [];
  const all = box === "trash" ? [...state.data.received, ...state.data.sent].filter(m => m.hidden)
    : state.data[box].filter(m => !m.hidden);
  return project ? all.filter(m => m.project === project) : all;
}

async function refresh() {
  try {
    state.data = await api("/api/inbox");
    const waiting = mails("received", "").filter(m => m.unread).length;
    document.title = (waiting ? `(${waiting}) ` : "") + "Graph-MIND 요청함";
    document.getElementById("status").textContent = state.data.unreachable.length
      ? "연결 안 된 허브: " + state.data.unreachable.join(", ") + " · 그 허브의 최신 상태는 알 수 없음"
      : "최신 · " + new Date().toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" });
    render();
  } catch (error) {
    document.getElementById("status").textContent = token ? "불러오지 못함: " + error.message
      : "접근 토큰이 없습니다. graph-mind-inbox로 다시 여세요.";
  }
}

function render() {
  const count = (box, project) => mails(box, project).filter(m => box !== "trash" && m.unread).length
    || (box === "trash" ? mails(box, project).length : 0);
  const boxButton = box => el("button", { class: "box" + (state.box === box ? " on" : ""),
    onclick: () => { state.box = box; render(); } }, BOXES[box],
    el("span", { class: "n" + (box !== "trash" && count(box, "") ? " new" : "") }, count(box, "") || ""));
  const projectButton = name => el("button", { class: "box" + (state.project === name ? " on" : ""),
    onclick: () => { state.project = name; render(); } }, name || "모든 프로젝트",
    el("span", { class: "n" }, mails(state.box, name).length || ""));
  fill(document.getElementById("boxes"),
    boxButton("received"), boxButton("sent"), boxButton("trash"),
    el("div", { class: "label" }, "프로젝트"),
    projectButton(""), (state.data ? state.data.projects : []).map(projectButton));

  const list = document.getElementById("list");
  const shown = mails(state.box, state.project);
  if (!shown.length) {
    fill(list, el("div", { class: "empty" }, { received: "받은 검토 요청이 없습니다.",
      sent: "보낸 요청이 없습니다.", trash: "삭제함이 비어 있습니다." }[state.box]));
    return;
  }
  fill(list, shown.map(m => {
    const mine = state.data.sent.some(s => s.id === m.id);
    const status = decisionOf(m);
    return el("div", { class: "mail" + (m.unread && state.box !== "trash" ? " unread" : "") + (state.selected === m.id ? " on" : ""),
      onclick: () => select(m.id) },
      el("div", { class: "line1" }, el("span", { class: "from" }, mine ? "받는 사람: " + m.reviewer : m.requester),
        el("span", { class: "chip " + status }, STATUS[status] || status),
        el("span", { class: "date" }, when(m.responded_at || m.requested_at))),
      el("div", { class: "subject" }, `${m.name}${m.version ? " v" + m.version : ""} 확인 요청`),
      el("div", { class: "preview" }, el("span", { class: "chip project" }, m.project), " ",
        mine && m.response ? (m.response.note || STATUS[status]) : recorded(m.focus) || recorded(m.note)));
  }));
}

function select(id) {
  state.selected = id;
  history.replaceState(null, "", "/#review/" + id);
  render();
  showReview(id);
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
  const note = el("textarea", { placeholder: "답장 (수정 요청이면 무엇을 고쳐야 하는지)" });
  const decide = async (decision) => {
    if (decision === "changes_requested" && !note.value.trim()) { flash("수정할 내용을 적어 주세요."); note.focus(); return; }
    try {
      await api("/api/respond", { review_id: id, decision, note: note.value, version: artifact.version });
      flash({ approved: `${label} 승인을 보냈습니다.`, changes_requested: "수정 요청을 보냈습니다.",
        withdrawn: "승인을 취소했습니다.", cancelled: "요청을 취소했습니다." }[decision]);
      await refresh(); showReview(id);
    } catch (error) { flash(error.message); }
  };
  const ask = `${review.project} 프로젝트의 검토 요청 ${id}(${label}) 열어서 확인할 것 기준으로 검토해줘.`;
  const summary = recorded(data.context.publishers_account.what);
  // the whole exchange about this result, oldest first, each answer under its request
  const exchange = [...(data.history.reviews_of_this_result || []).filter(r => r.id !== id),
    { ...review, version: artifact.version }].sort((a, b) => String(a.requested_at).localeCompare(String(b.requested_at)));
  const answers = r => [...(r.earlier_responses || []), ...(r.response ? [{ ...r.response, at: r.responded_at }] : [])];
  const thread = exchange.flatMap(r => [
    el("div", { class: "msg" + (r.id === id ? " now" : "") },
      el("div", { class: "who" }, `${r.requester} → ${r.reviewer} · ${when(r.requested_at, true)}`),
      el("div", { class: "title" }, `${name}${r.version ? " v" + r.version : ""} 확인 요청`),
      recorded(r.focus) ? el("div", {}, r.focus) : null,
      r.note ? el("div", { class: "meta" }, r.note) : null,
      r.id === id && summary ? el("div", { class: "meta" }, "바꾼 내용: " + summary) : null),
    answers(r).map(a => el("div", { class: "msg reply" },
      el("div", { class: "who" }, `${a.by || r.reviewer} · ${when(a.at, true)}`),
      el("div", { class: "title" }, el("span", { class: "chip " + a.decision }, STATUS[a.decision] || a.decision)),
      a.note ? el("div", {}, a.note) : null)),
    r.status === "cancelled" ? el("div", { class: "msg reply" }, el("div", { class: "meta" }, "요청이 취소됐습니다.")) : null]);
  const changes = data.changes || {};
  const diff = changes.changes && changes.changes.diff;
  const canAnswer = data.mine_to_answer && review.status !== "cancelled";
  fill(read,
    el("div", { class: "sub" }, el("span", { class: "chip project" }, review.project), " ",
      el("span", { class: "chip " + status }, STATUS[status] || status)),
    el("h2", {}, `${label} 확인 요청`),
    el("div", { class: "row" },
      artifact.artifact_id ? el("button", { class: "act", onclick: async () => {
        try { const r = await api("/api/open", { review_id: id }); flash(`${r.opened} v${r.version}을(를) 열었습니다.`); }
        catch (error) { flash(error.message); } } }, "파일 열기") : null,
      el("button", { class: "act", title: ask, onclick: async () => {
        try { await navigator.clipboard.writeText(ask); flash("복사했습니다. Claude나 Codex에 붙여넣으세요."); }
        catch (e) { prompt("AI에게 붙여넣으세요", ask); } } }, "AI에게 맡기기"),
      !data.mine_to_answer && (review.status === "requested" || review.status === "in_review")
        ? el("button", { class: "act danger", onclick: () => decide("cancelled") }, "요청 취소") : null,
      el("button", { class: "act quiet", onclick: async () => {
        try { await api("/api/mark", { review_id: id, hidden: !hidden }); flash(hidden ? "복원했습니다." : "삭제함으로 옮겼습니다. 상대방 기록은 그대로입니다.");
          await refresh(); showReview(id); } catch (error) { flash(error.message); } } }, hidden ? "복원" : "삭제")),
    el("div", { class: "thread" }, thread),
    diff ? el("h3", {}, `바뀐 점 (v${changes.from} → v${changes.to}, 파일에서 직접 비교)`) : null,
    diff ? el("div", { class: "diff" }, diff.map(line => el("div", { class:
      line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "" }, line))) : null,
    canAnswer ? el("h3", {}, "답장") : null,
    canAnswer ? el("div", { class: "card" },
      el("div", { class: "meta" }, review.status === "approved"
        ? `${label}을(를) 승인했습니다. 결정을 바꾸려면 아래에서 승인을 취소하거나 수정 요청을 보내세요.`
        : `파일을 확인한 뒤 답장하세요. ${label}에만 적용됩니다. AI에게 "승인해" / "수정 요청해"라고 해도 됩니다.`),
      note,
      el("div", { class: "row" },
        review.status !== "approved" ? el("button", { class: "act primary", onclick: () => decide("approved") }, "승인") : null,
        el("button", { class: "act danger", onclick: () => decide("changes_requested") }, "수정 요청"),
        review.status === "approved" ? el("button", { class: "act", onclick: () => decide("withdrawn") }, "승인 취소") : null)) : null);
}

(async () => {
  await refresh();
  if (state.selected) {
    if (state.data && state.data.sent.some(m => m.id === state.selected)) state.box = "sent";
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
