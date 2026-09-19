"""MERIDIAN CORE 4.2 -- a fixture stand-in for a legacy core-banking console.

Stands in for the class of application this project targets: a server-rendered
back-office app with no API, hostile markup, real session handling, and the
runtime failure modes that actually break automation in production.

Test-only control plane (``/__fault``) lets a caller arm the exceptional states
on demand, so replay error handling can be demonstrated rather than described.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import render
from .data import MIN_INITIAL_DEPOSIT, SUB_ACCOUNT_TYPES, MemberStore, OPERATORS
from .tenants import DEFAULT_TENANT, TENANTS, Tenant

SESSION_COOKIE = "MCSESS"

# Faults the fixture can be told to inject. Each is a runtime condition that a
# stable enterprise UI still produces every day.
FAULT_KINDS = {
    "session_expired",   # session dies mid-flow
    "slow",              # transient slowness
    "app_error",         # HTTP 500 from the app
    "interstitial",      # unexpected modal blocks the flow
    "permission_denied",  # operator lacks rights on this record
}


@dataclass
class Session:
    sid: str
    operator: str
    created: float = field(default_factory=time.time)
    seen_interstitial: bool = False


@dataclass
class Fault:
    kind: str
    remaining: int = 1
    seconds: float = 3.0
    message: str = ""


class AppState:
    def __init__(self, tenant: Tenant, session_ttl: float) -> None:
        self.tenant = tenant
        self.session_ttl = session_ttl
        self.store = MemberStore()
        self.sessions: dict[str, Session] = {}
        self.faults: list[Fault] = []
        self.lock = threading.Lock()

    # -- fault plumbing -------------------------------------------------
    def arm(self, kind: str, count: int = 1, seconds: float = 3.0, message: str = "") -> None:
        with self.lock:
            self.faults.append(Fault(kind, count, seconds, message))

    def take(self, kind: str) -> Fault | None:
        """Consume one charge of an armed fault, if any."""
        with self.lock:
            for f in self.faults:
                if f.kind == kind and f.remaining > 0:
                    f.remaining -= 1
                    return f
            return None

    def clear(self) -> None:
        with self.lock:
            self.faults.clear()


class Handler(BaseHTTPRequestHandler):
    state: AppState  # injected on the server instance

    server_version = "MeridianCore/4.2"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:  # keep test output readable
        if self.server.verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # -- plumbing -------------------------------------------------------
    @property
    def st(self) -> AppState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, payload: dict, status: int = 200) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, to: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(302)
        self.send_header("Location", to)
        self.send_header("Content-Length", "0")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _form(self) -> dict[str, str]:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8") if n else ""
        return {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}

    def _session(self) -> Session | None:
        cookie = self.headers.get("Cookie") or ""
        sid = ""
        for part in cookie.split(";"):
            k, _, v = part.strip().partition("=")
            if k == SESSION_COOKIE:
                sid = v
        s = self.st.sessions.get(sid)
        if s and (time.time() - s.created) > self.st.session_ttl:
            self.st.sessions.pop(sid, None)
            return None
        return s

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        self._route("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._route("POST")

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        path, q = url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
        t = self.st.tenant

        # --- test-only control plane (never part of the automated surface) ---
        if path == "/__fault":
            if method == "POST":
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip()
                if ctype == "application/json":
                    n = int(self.headers.get("Content-Length") or 0)
                    try:
                        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
                    except json.JSONDecodeError:
                        return self._json({"ok": False, "error": "invalid JSON body"}, 400)
                else:
                    body = self._form() or {}
                kind = body.get("kind", "")
                if kind == "clear":
                    self.st.clear()
                    return self._json({"ok": True, "faults": []})
                if kind not in FAULT_KINDS:
                    return self._json({"ok": False, "error": f"unknown fault {kind!r}",
                                       "known": sorted(FAULT_KINDS)}, 400)
                self.st.arm(kind, int(body.get("count", 1)),
                            float(body.get("seconds", 3.0)), body.get("message", ""))
                return self._json({"ok": True, "armed": kind})
            return self._json({"faults": [f.__dict__ for f in self.st.faults]})
        if path == "/__health":
            return self._json({"ok": True, "tenant": t.tenant_id, "product": t.product})

        # --- injected transient slowness, before anything else ---
        slow = self.st.take("slow")
        if slow:
            time.sleep(slow.seconds)

        # --- injected hard app error ---
        if self.st.take("app_error"):
            return self._send(render.error_page(
                t, "SYS-0500", "An unexpected condition occurred while processing the "
                               "request. Contact the service desk with this reference."), 500)

        if path == "/":
            return self._send(render.login_page(t))

        if path == "/signon" and method == "POST":
            f = self._form()
            uid, pwd = f.get("f_uid", ""), f.get("f_pwd", "")
            if OPERATORS.get(uid) != pwd:
                return self._send(render.login_page(t, "Sign-on failed. Check your "
                                                       "operator ID and passcode."), 401)
            sid = uuid.uuid4().hex
            self.st.sessions[sid] = Session(sid, uid)
            return self._redirect("/console", {
                "Set-Cookie": f"{SESSION_COOKIE}={sid}; Path=/; HttpOnly"})

        if path == "/signoff":
            s = self._session()
            if s:
                self.st.sessions.pop(s.sid, None)
            return self._redirect("/")

        # --- everything past here needs a live session ---
        session = self._session()
        if session is None or self.st.take("session_expired"):
            if session:
                self.st.sessions.pop(session.sid, None)
            return self._send(render.login_page(
                t, "Your session has expired. Please sign on again."), 200)

        # --- tenant-configured post-sign-on notice ---
        if (t.login_interstitial and not session.seen_interstitial
                and path.startswith("/members")):
            session.seen_interstitial = True
            return self._send(render.interstitial_page(t, t.login_interstitial, path))

        # --- injected unexpected modal ---
        # Check the path *before* consuming a charge: a frameset shell issues
        # several requests per screen, and spending the charge on /frame/nav
        # would mean the modal never lands where the test aimed it.
        fault = self.st.take("interstitial") if path.startswith("/members") else None
        if fault:
            msg = fault.message or ("Scheduled maintenance window begins at 22:00 ET. "
                                    "Batch postings may be delayed.")
            return self._send(render.interstitial_page(t, msg, path))

        if path == "/console":
            return self._send(render.console_frameset(t))
        if path == "/frame/banner":
            return self._send(render.banner_frame(t, session.operator))
        if path == "/frame/nav":
            return self._send(render.nav_frame(t))
        if path.startswith("/stub/"):
            return self._send(render.stub_page(t, path.rsplit("/", 1)[-1].title()))

        if path == "/members/search":
            return self._send(render.search_page(t))

        if path == "/members/detail":
            mid = (q.get("f_mid") or "").strip()
            if not mid:
                return self._send(render.search_page(
                    t, f"{t.member_id_label} is required."))
            member = self.st.store.get(mid)
            if member is None:
                # A legitimate business outcome, not a crash.
                return self._send(render.search_page(
                    t, f"No member record found for {t.member_id_label} {mid}.", mid))
            if member.restricted or self.st.take("permission_denied"):
                return self._send(render.error_page(
                    t, "SEC-0041",
                    "You are not authorized to service this member relationship "
                    "(PRIVACY HOLD). Contact the compliance desk."), 403)
            return self._send(render.member_detail_page(t, member))

        if path == "/members/subaccount/new":
            member = self.st.store.get((q.get("f_mid") or "").strip())
            if member is None:
                return self._send(render.search_page(t, "Select a member first."))
            return self._send(render.subaccount_form_page(t, member))

        if path == "/members/subaccount/review" and method == "POST":
            f = self._form()
            member = self.st.store.get(f.get("f_mid", ""))
            if member is None:
                return self._send(render.search_page(t, "Select a member first."))
            kind, nick, amt = f.get("f_type", ""), f.get("f_nick", ""), f.get("f_amt", "")
            err = _validate_sub_account(kind, nick, amt)
            if err:
                return self._send(render.subaccount_form_page(t, member, err, kind, nick, amt))
            return self._send(render.subaccount_review_page(t, member, kind, nick, float(amt)))

        if path == "/members/subaccount/commit" and method == "POST":
            f = self._form()
            member = self.st.store.get(f.get("f_mid", ""))
            if member is None:
                return self._send(render.search_page(t, "Select a member first."))
            acct = self.st.store.open_sub_account(
                member.member_id, f.get("f_type", "SAVINGS"), float(f.get("f_amt", "0")))
            return self._send(render.subaccount_done_page(t, member, acct))

        return self._send(render.error_page(t, "SYS-0404", f"No handler for {path}."), 404)


def _validate_sub_account(kind: str, nickname: str, amount: str) -> str:
    if kind not in SUB_ACCOUNT_TYPES:
        return "Select a valid account type."
    if not nickname.strip():
        return "Nickname is required."
    try:
        value = float(amount)
    except ValueError:
        return "Initial deposit must be a numeric amount."
    if value < MIN_INITIAL_DEPOSIT:
        return f"Initial deposit must be at least ${MIN_INITIAL_DEPOSIT:,.2f}."
    return ""


def build_server(port: int = 8799, tenant_id: str = DEFAULT_TENANT,
                 session_ttl: float = 3600.0, verbose: bool = False) -> ThreadingHTTPServer:
    tenant = TENANTS[tenant_id]
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.state = AppState(tenant, session_ttl)  # type: ignore[attr-defined]
    httpd.verbose = verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True
    return httpd


def main() -> None:
    ap = argparse.ArgumentParser(description="MERIDIAN CORE fixture app")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--tenant", default=DEFAULT_TENANT, choices=sorted(TENANTS))
    ap.add_argument("--session-ttl", type=float, default=3600.0)
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()
    httpd = build_server(a.port, a.tenant, a.session_ttl, a.verbose)
    print(f"MERIDIAN CORE [{a.tenant}] on http://127.0.0.1:{a.port}  "
          f"(operator svc_automation)")
    httpd.serve_forever()


if __name__ == "__main__":
    main()
