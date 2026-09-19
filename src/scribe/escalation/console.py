"""A minimal but real operator console.

It is deliberately not a polished co-browsing product -- that was out of scope.
What is real is the mechanism underneath it: the operator sees and drives the
*same live session* the automation was using, not a copy and not a fresh one.
Frames are screenshots of that page; clicks and keystrokes are forwarded into
it through the session host; and every one of them is recorded against the
intervention as a human action.

The control token is checked on every forwarded input, so a stale browser tab
cannot drive a session whose lease has expired or that automation has taken
back.
"""

from __future__ import annotations

import threading
from typing import Any

from pydantic import BaseModel

from .broker import EscalationBroker, Resolution
from .control import ControlOwner
from .session_host import SessionHost

class ClaimBody(BaseModel):
    operator: str = "operator"


class InputBody(BaseModel):
    """One forwarded interaction. Validated before it reaches the session."""

    operator: str = "operator"
    kind: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    url: str | None = None


class ResolveBody(BaseModel):
    operator: str = "operator"
    resolution: str = "resume"
    note: str = ""


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Scribe - operator console</title>
<style>
 :root { color-scheme: light dark; --fg:#1a1a1a; --bg:#f4f5f7; --card:#fff;
         --line:#d7dae0; --accent:#1f3864; --warn:#8b0000; }
 @media (prefers-color-scheme: dark) { :root {
   --fg:#e8e8ea; --bg:#17181b; --card:#212329; --line:#3a3d45; --accent:#8fa8dd; } }
 body { margin:0; font:13px/1.5 ui-sans-serif,system-ui,sans-serif;
        background:var(--bg); color:var(--fg); }
 header { background:var(--accent); color:#fff; padding:10px 16px; font-weight:600; }
 .wrap { display:flex; gap:16px; padding:16px; align-items:flex-start;
         flex-wrap:wrap; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:8px;
         padding:14px; }
 #side { width:380px; flex:0 0 380px; }
 #screen { border:1px solid var(--line); border-radius:6px; cursor:crosshair;
           max-width:100%; display:block; background:#fff; }
 .pill { display:inline-block; padding:2px 8px; border-radius:99px; font-size:11px;
         font-weight:600; border:1px solid var(--line); }
 .automation { background:#e6f0ff; color:#1f3864; }
 .human { background:#e7f7ec; color:#155724; }
 .handoff_pending { background:#fff3cd; color:#7a5b00; }
 pre { white-space:pre-wrap; word-break:break-word; font-size:12px; margin:6px 0;
       max-height:230px; overflow:auto; }
 button { font:inherit; padding:6px 12px; border-radius:6px; cursor:pointer;
          border:1px solid var(--line); background:var(--card); color:var(--fg); }
 button.primary { background:var(--accent); color:#fff; border-color:var(--accent); }
 button:disabled { opacity:.45; cursor:not-allowed; }
 input[type=text] { font:inherit; padding:6px 8px; width:100%; box-sizing:border-box;
                    border:1px solid var(--line); border-radius:6px;
                    background:var(--bg); color:var(--fg); }
 h2 { font-size:13px; margin:14px 0 6px; text-transform:uppercase;
      letter-spacing:.04em; opacity:.7; }
 .muted { opacity:.65; }
 .row { display:flex; gap:8px; margin:8px 0; flex-wrap:wrap; }
</style></head><body>
<header>Scribe &mdash; operator console</header>
<div class="wrap">
  <div>
    <div class="card">
      <div class="row" style="justify-content:space-between;align-items:center">
        <span>Live session <span class="muted" id="url"></span></span>
        <span class="pill" id="owner">…</span>
      </div>
      <img id="screen" alt="live session">
      <div class="muted" style="margin-top:6px">
        Click the image to click the page. Type below and press Send.
      </div>
      <div class="row">
        <input type="text" id="text" placeholder="text to type into the focused field">
        <button id="send">Send</button>
        <button data-key="Enter">Enter</button>
        <button data-key="Tab">Tab</button>
        <button data-key="Escape">Esc</button>
      </div>
    </div>
  </div>
  <div id="side">
    <div class="card">
      <h2 style="margin-top:0">Intervention</h2>
      <pre id="briefing">waiting…</pre>
      <div class="row">
        <button class="primary" id="claim">Take control</button>
        <button id="resume">Hand back &amp; resume</button>
      </div>
      <div class="row">
        <button id="approve">Approve step</button>
        <button id="deny">Decline</button>
        <button id="done">I finished it</button>
        <button id="abort">Abort run</button>
      </div>
      <h2>Human actions recorded</h2>
      <pre id="actions">(none yet)</pre>
    </div>
  </div>
</div>
<script>
const $ = (id) => document.getElementById(id);
let operator = localStorage.getItem('scribe-operator') ||
               ('operator-' + Math.random().toString(36).slice(2, 7));
localStorage.setItem('scribe-operator', operator);
let holding = false;

async function post(path, body) {
  const r = await fetch(path, {method:'POST', headers:{'Content-Type':'application/json'},
                               body: JSON.stringify({operator, ...(body||{})})});
  const d = await r.json();
  if (!r.ok) alert(d.detail || d.error || 'request failed');
  return d;
}
async function refresh() {
  try {
    const s = await (await fetch('/api/state')).json();
    const owner = s.control.owner;
    holding = owner === 'human' && s.control.holder === 'operator:' + operator;
    $('owner').textContent = owner.replace('_',' ');
    $('owner').className = 'pill ' + owner;
    $('url').textContent = s.url || '';
    $('briefing').textContent = s.briefing || 'No open intervention. Automation is running.';
    $('actions').textContent = (s.human_actions || []).map(
      a => `${a.kind}: ${a.detail}`).join('\\n') || '(none yet)';
    $('claim').disabled = owner !== 'handoff_pending';
    for (const id of ['resume','approve','deny','done','abort'])
      $(id).disabled = !holding;
    $('screen').src = '/api/screen?t=' + Date.now();
  } catch (e) { /* the run ended; keep polling */ }
}
$('screen').addEventListener('click', async (ev) => {
  if (!holding) return alert('Take control first.');
  const img = ev.target, r = img.getBoundingClientRect();
  const x = (ev.clientX - r.left) / r.width * img.naturalWidth;
  const y = (ev.clientY - r.top) / r.height * img.naturalHeight;
  await post('/api/input', {kind:'click', x, y});
  refresh();
});
$('send').onclick = async () => {
  if (!holding) return alert('Take control first.');
  await post('/api/input', {kind:'type', text: $('text').value});
  $('text').value = ''; refresh();
};
document.querySelectorAll('button[data-key]').forEach(b => b.onclick = async () => {
  if (!holding) return alert('Take control first.');
  await post('/api/input', {kind:'key', key: b.dataset.key}); refresh();
});
$('claim').onclick = async () => { await post('/api/claim'); refresh(); };
const resolve = (r) => async () => { await post('/api/resolve', {resolution:r}); refresh(); };
$('resume').onclick  = resolve('resume');
$('approve').onclick = resolve('approved');
$('deny').onclick    = resolve('denied');
$('done').onclick    = resolve('completed_by_human');
$('abort').onclick   = resolve('abort');
setInterval(refresh, 1000); refresh();
</script></body></html>
"""


def build_app(broker: EscalationBroker, host: SessionHost | None = None):
    """FastAPI app over one live session."""
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import HTMLResponse, JSONResponse, Response

    app = FastAPI(title="Scribe operator console", docs_url=None, redoc_url=None)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _PAGE

    @app.get("/api/state")
    def state():
        current = broker.current
        info = {
            "control": broker.token.snapshot(),
            "briefing": current.briefing() if current else "",
            "intervention": current.to_dict() if current else None,
            "human_actions": [a.to_dict() for a in (current.human_actions if current else [])],
            "url": "",
        }
        if host is not None:
            try:
                info["url"] = host.surface.page.url
            except Exception:
                pass
        return JSONResponse(info)

    @app.get("/api/screen")
    def screen():
        if host is None:
            raise HTTPException(503, "no live session attached to this console")
        png = host.screenshot()
        if png is None:
            raise HTTPException(503, "no frame available yet")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    @app.get("/api/controls")
    def controls():
        """What the automation can see on the live screen, with positions."""
        if host is None:
            raise HTTPException(503, "no live session attached to this console")
        try:
            return JSONResponse(host.submit("observe").wait(timeout=20.0))
        except Exception as exc:
            raise HTTPException(504, str(exc))

    @app.post("/api/claim")
    def claim(body: ClaimBody):
        try:
            req = broker.claim(body.operator)
        except Exception as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True, "intervention": req.id})

    @app.post("/api/input")
    def send_input(body: InputBody):
        # Authority first, before anything about the session is revealed, and on
        # every single input rather than once at claim time: a lease can expire,
        # or automation can have taken the session back, while an operator's tab
        # sits open with the buttons still enabled.
        try:
            broker.token.assert_human(body.operator)
        except Exception as exc:
            raise HTTPException(409, str(exc))
        if host is None:
            raise HTTPException(503, "no live session attached to this console")
        if body.kind not in ("click", "type", "key", "navigate"):
            raise HTTPException(400, f"unsupported input {body.kind!r}")
        payload = {k: v for k, v in body.model_dump().items()
                   if k not in ("operator", "kind") and v is not None}
        try:
            result = host.submit(body.kind, **payload).wait(timeout=20.0)
        except Exception as exc:
            raise HTTPException(504, str(exc))
        return JSONResponse({"ok": True, "result": result})

    @app.post("/api/resolve")
    def resolve(body: ResolveBody):
        try:
            resolution = Resolution(body.resolution)
        except ValueError:
            raise HTTPException(400, f"unknown resolution {body.resolution!r}")
        try:
            broker.resolve(body.operator, resolution, body.note)
        except Exception as exc:
            raise HTTPException(409, str(exc))
        return JSONResponse({"ok": True, "resolution": resolution.value})

    return app


class ConsoleSink:
    """Routes interventions to the console, and serves it in a background thread.

    The console runs in-process next to the run so it can share the live
    session object directly. In a deployment the session host would sit behind
    an RPC boundary and the console would be its own service -- the command
    queue in `SessionHost` is already that seam; only the transport changes.
    """

    def __init__(self, broker: EscalationBroker, host: SessionHost,
                 *, host_addr: str = "127.0.0.1", port: int = 8900) -> None:
        self.broker, self.session_host = broker, host
        self.addr, self.port = host_addr, port
        self._server = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.addr}:{self.port}/"

    def start(self) -> str:
        import uvicorn

        app = build_app(self.broker, self.session_host)
        config = uvicorn.Config(app, host=self.addr, port=self.port,
                                log_level="warning", access_log=False)
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        return self.url

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- InterventionSink -------------------------------------------------
    def publish(self, request) -> None:
        print(f"\n  >> operator console: {self.url}  "
              f"(intervention {request.id})\n", flush=True)

    def describe(self) -> str:
        return self.url


def serve_standalone(host: str = "127.0.0.1", port: int = 8900) -> int:
    """Run the console with no live session attached.

    Useful for looking at the interface; there is nothing to drive, because a
    session only exists for the duration of a run.
    """
    import uvicorn

    from .control import ControlToken

    broker = EscalationBroker(ControlToken(), sinks=[])
    print(f"operator console on http://{host}:{port}/ (no live session attached)")
    uvicorn.run(build_app(broker, None), host=host, port=port, log_level="warning")
    return 0
