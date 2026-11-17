"""
Offline check for the service worker (frontend/src/sw.js), run on the Pi:

    cd <dev worktree> && (cd frontend && npm run build)
    .venv/bin/uvicorn main:app --app-dir backend --port 8001 --host 127.0.0.1 &   # jobs off in its .env
    .venv/bin/python3 -I -u frontend/test/offline_test_ff.py

Headless Firefox over WebDriver BiDi against that backend: load the app
online, let the worker install and fill its caches, STOP the backend (real
offline, no emulation), reload, and check the calendar and Plan tab still
render from the cache with the Offline banner; then start the backend again
and check the banner clears. Headless Chromium hangs on the Pi; Firefox works.
Navigating to the app URL again would be a fragment navigation (hash routes),
so the offline step uses browsingContext.reload.
"""
import asyncio, json, os, shutil, signal, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
import websockets

BASE = "http://127.0.0.1:8001"
DEV = str(Path(__file__).resolve().parents[2])  # the checkout this script lives in
UVICORN = [f"{DEV}/.venv/bin/uvicorn", "main:app", "--app-dir", "backend", "--port", "8001", "--host", "127.0.0.1"]
LOG = os.environ.get("SWTEST_LOG", "/tmp/calendar-optimizer-8001.log")  # uvicorn output when the test restarts it
PORT = 9224

def server_up():
    try:
        return urllib.request.urlopen(f"{BASE}/health", timeout=2).status == 200
    except Exception:
        return False

def stop_server():
    subprocess.run(["pkill", "-f", "uvicorn main:app --app-dir backend --port 8001"], check=False)
    for _ in range(40):
        if not server_up(): return
        time.sleep(0.25)
    raise RuntimeError("server still up")

def start_server():
    subprocess.Popen(UVICORN, cwd=DEV, stdout=open(LOG, "a"), stderr=subprocess.STDOUT, start_new_session=True)
    for _ in range(80):
        if server_up(): return
        time.sleep(0.25)
    raise RuntimeError("server did not come up")

class BiDi:
    def __init__(self, ws): self.ws, self.n = ws, 0
    async def send(self, method, **params):
        self.n += 1; mid = self.n
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == mid:
                if msg.get("type") == "error": raise RuntimeError(f"{method}: {msg.get('error')}: {msg.get('message')}")
                return msg.get("result", {})
    async def eval(self, expr):
        """Evaluate in the page; the expression must produce a JSON string (or a promise of one)."""
        r = await self.send("script.evaluate", expression=expr, target={"context": self.ctx}, awaitPromise=True, resultOwnership="none")
        if r.get("type") == "exception":
            raise RuntimeError("page exception: " + json.dumps(r.get("exceptionDetails", {}))[:400])
        v = r.get("result", {})
        return json.loads(v["value"]) if v.get("type") == "string" else v.get("value")
    async def nav(self, url):
        await self.send("browsingContext.navigate", context=self.ctx, url=url, wait="complete")
    async def reload(self):
        # A navigate to the same URL (hash routes!) is a fragment navigation and reloads nothing.
        await self.send("browsingContext.reload", context=self.ctx, wait="complete")

STATE = """JSON.stringify({
  events: document.querySelectorAll('.sx__event, .sx__month-grid-event, .sx__time-grid-event').length,
  banner: (document.querySelector('.offline-banner') || {}).textContent || '',
  error: (document.querySelector('.error-card') || {}).textContent || '',
  title: (document.querySelector('.page-title') || {}).textContent || '',
  planEls: document.querySelectorAll('.pane-plan [class^="rv-"]').length,
  planText: (document.querySelector('.pane-plan') || {}).textContent?.slice(0, 160) || '',
})"""

async def wait_for(b, expr, timeout=25, desc=""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = await b.eval(expr)
        if v: return v
        await asyncio.sleep(0.5)
    raise AssertionError(f"timeout waiting for {desc or expr}")

RENDERED = "JSON.stringify(document.querySelectorAll('.sx__event, .sx__month-grid-event, .sx__time-grid-event').length > 0 || !!document.querySelector('.error-card'))"

async def main():
    assert server_up(), "dev backend on 8001 must be running first"
    profile = tempfile.mkdtemp(prefix="ffsw-")
    env = dict(os.environ, TZ="America/New_York", MOZ_HEADLESS="1")
    ff = subprocess.Popen(["firefox", "--headless", "--profile", profile, "--no-remote", f"--remote-debugging-port={PORT}", "--window-size=1200,900", "about:blank"],
                          env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    try:
        ws = None
        for _ in range(80):
            try:
                ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/session", max_size=None); break
            except Exception: await asyncio.sleep(0.5)
        assert ws, "could not reach Firefox BiDi"
        async with ws:
            b = BiDi(ws)
            await b.send("session.new", capabilities={"alwaysMatch": {}})
            tree = await b.send("browsingContext.getTree")
            b.ctx = tree["contexts"][0]["context"]

            print("1. online load")
            await b.nav(f"{BASE}/#/calendar")
            await wait_for(b, RENDERED, desc="first render")
            st = await b.eval(STATE); print("   ", {k: st[k] for k in ('events', 'banner', 'error')})
            assert st["events"] > 0 and not st["banner"] and not st["error"]

            print("2. service worker installs and takes control")
            reg = await b.eval("navigator.serviceWorker.ready.then(r => JSON.stringify({scope: r.scope, state: r.active && r.active.state}))")
            print("   ", reg); assert reg["state"] == "activated"
            await wait_for(b, "JSON.stringify(!!navigator.serviceWorker.controller)", desc="controller")
            for p in ("/events", "/review/summary", "/events/preview", "/optimizer/runs?limit=6"):
                ok = await b.eval(f"fetch('{p}').then(r => JSON.stringify(r.ok))"); assert ok, p
            caches = await b.eval("caches.keys().then(async ks => { const out = {}; for (const k of ks) out[k] = (await (await caches.open(k)).keys()).map(r => new URL(r.url).pathname + new URL(r.url).search); return JSON.stringify(out) })")
            print("   ", json.dumps(caches))
            static = next((v for k, v in caches.items() if k.startswith("static-")), [])
            assert "/" in static and any(p.startswith("/assets/") for p in static) and "/manifest.webmanifest" in static, "precache incomplete"
            assert {"/events", "/review/summary", "/events/preview"} <= set(caches.get("data-v1", [])), "data cache missing"
            stamp = await b.eval("fetch('/events').then(r => JSON.stringify(r.headers.get('X-SW-Cached-At')))")
            assert stamp is None, f"live response carried a cache stamp: {stamp}"

            print("3. backend stopped, page reloaded")
            stop_server()
            await b.reload()
            await wait_for(b, RENDERED, desc="offline render")
            assert await b.eval("JSON.stringify(performance.getEntriesByType('navigation').length)") >= 1
            await asyncio.sleep(2)
            off = await b.eval(STATE); print("   ", {k: off[k] for k in ('events', 'banner', 'error')})
            assert off["events"] > 0, "no events rendered offline"
            assert off["banner"].startswith("Offline.") and "saved" in off["banner"], f"banner: {off['banner']!r}"
            assert not off["error"], f"error card offline: {off['error']}"
            stamp = await b.eval("fetch('/events').then(r => JSON.stringify(r.headers.get('X-SW-Cached-At')))")
            assert stamp, "offline /events not served from cache"
            nocopy = await b.eval("fetch('/scout/status').then(r => JSON.stringify({status: r.status, off: r.headers.get('X-SW-Offline')}))")
            print("    never-cached path ->", nocopy); assert nocopy["status"] == 503 and nocopy["off"] == "1"

            print("4. Plan tab offline")
            await b.eval("JSON.stringify(location.hash = '#/plan')"); await asyncio.sleep(2.5)
            plan = await b.eval(STATE); print("   ", {k: plan[k] for k in ('title', 'planEls', 'banner')}, plan["planText"][:100])
            assert plan["title"] == "Plan" and plan["planEls"] > 0

            print("5. a decision offline fails clearly")
            msg = await b.eval("fetch('/optimizer/proposals/nope/reject', {method:'POST'}).then(r => JSON.stringify('response ' + r.status), e => JSON.stringify('rejected: ' + e.message))")
            print("   ", msg); assert msg.startswith("rejected")

            print("6. backend back, banner clears")
            start_server()
            await b.eval("JSON.stringify(window.dispatchEvent(new Event('online')))")
            await wait_for(b, "JSON.stringify(!document.querySelector('.offline-banner'))", desc="banner cleared")
            print("    cleared")
            print("ALL CHECKS PASSED")
    finally:
        try: os.killpg(ff.pid, signal.SIGTERM)
        except Exception: pass
        ff.wait(timeout=15)
        shutil.rmtree(profile, ignore_errors=True)
        if not server_up():
            print("restarting dev backend after test"); start_server()

if __name__ == "__main__":
    asyncio.run(main())
