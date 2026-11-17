"""
Capture the docs/ screenshots from the demo server (serve_demo.sh, port 8002)
with headless Chrome over the DevTools protocol. Writes PNGs to docs/screenshots/:
desktop shots at 1600x1000, phone shots at 780 px wide (390 pt at 2x).

    .venv/bin/python3 tools/screenshots/capture.py [--out DIR] [--chrome PATH]

Needs `websockets` (in the venv) and Chrome or Chromium.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import datetime as dt
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

import websockets

REPO = Path(__file__).resolve().parents[2]
APP = "http://127.0.0.1:8002/"
PORT = 9333
CHROMES = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "chromium", "google-chrome"]

# Clicks the first visible element matching `sel` whose text starts with `text`.
CLICK = """(sel, text) => {
  const els = [...document.querySelectorAll(sel)].filter(e => e.offsetParent !== null);
  const el = text == null ? els[0] : els.find(e => e.textContent.trim().startsWith(text));
  if (!el) return 'missing ' + sel + ' ' + text;
  el.click(); return 'ok';
}"""


def find_chrome(explicit: str | None) -> str:
    for c in [explicit] if explicit else CHROMES:
        if c and (Path(c).exists() or shutil.which(c)):
            return c
    raise SystemExit("Chrome not found; pass --chrome PATH")


async def capture(out: Path) -> None:
    for _ in range(50):
        try:
            pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json"))
            ws_url = next(p["webSocketDebuggerUrl"] for p in pages if p["type"] == "page")
            break
        except Exception:
            time.sleep(0.2)
    else:
        raise SystemExit("headless Chrome did not start")

    async with websockets.connect(ws_url, max_size=50_000_000) as ws:
        n = 0

        async def cdp(method, **params):
            nonlocal n
            n += 1
            await ws.send(json.dumps({"id": n, "method": method, "params": params}))
            while True:
                msg = json.loads(await ws.recv())
                if msg.get("id") == n:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        async def click(sel, text=None, times=1):
            for _ in range(times):
                r = await cdp("Runtime.evaluate", returnByValue=True,
                              expression=f"({CLICK})({json.dumps(sel)}, {json.dumps(text)})")
                if r.get("result", {}).get("value") != "ok":
                    raise RuntimeError(r)
                await asyncio.sleep(0.6)
            await asyncio.sleep(1.0)

        async def size(w, h, scale, mobile=False):
            await cdp("Emulation.setDeviceMetricsOverride", width=w, height=h, deviceScaleFactor=scale, mobile=mobile)
            await cdp("Emulation.setTouchEmulationEnabled", enabled=mobile)

        async def go(route, wait=3.0):
            await cdp("Page.navigate", url=f"{APP}?r={time.time()}{route}")
            await asyncio.sleep(wait)

        async def shot(name):
            data = (await cdp("Page.captureScreenshot", format="png"))["data"]
            (out / f"{name}.png").write_bytes(base64.b64decode(data))
            print("saved", out / f"{name}.png")

        await cdp("Page.enable")
        await cdp("Runtime.enable")
        await cdp("Emulation.setEmulatedMedia", features=[{"name": "prefers-color-scheme", "value": "light"}])

        # The demo week is the coming Monday to Sunday (seed_demo.py).
        today = dt.datetime.now(ZoneInfo("America/New_York")).date()
        to_next_wednesday = (7 - today.weekday()) + 2

        await size(1280, 800, 1.25)
        await go("#/calendar?preview=1")
        await click(".cnav-views button", "Week")
        await click(".cnav-next")
        await shot("calendar-week")
        for tab in ("plan", "events", "hours"):
            await go(f"#/{tab}")
            await shot(tab)

        await size(390, 844, 2, mobile=True)
        await go("#/calendar?preview=1", wait=3.5)
        await click(".cnav-views button", "Day")
        # ?preview=1 may jump to the first day with proposals; count from today instead.
        await click(".calendar-nav button", "Today")
        await click(".cnav-next", times=to_next_wednesday)
        await shot("phone-day")
        for tab in ("plan", "events", "hours"):
            await go(f"#/{tab}")
            await shot(f"phone-{tab}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=REPO / "docs" / "screenshots")
    ap.add_argument("--chrome")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    urllib.request.urlopen(APP + "health", timeout=5)  # fails fast when serve_demo.sh is not running

    profile = tempfile.mkdtemp(prefix="demo-chrome-")
    proc = subprocess.Popen([find_chrome(args.chrome), "--headless=new", f"--remote-debugging-port={PORT}",
                             f"--user-data-dir={profile}", "--no-first-run", "--hide-scrollbars", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        asyncio.run(capture(args.out))
    finally:
        proc.terminate()
        proc.wait()
        shutil.rmtree(profile, ignore_errors=True)


if __name__ == "__main__":
    main()
