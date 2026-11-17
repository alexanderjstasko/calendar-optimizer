# Screenshots for the docs/ site

The pictures in `docs/screenshots/` come from a demo copy of the app filled with a
made-up week, never from a real instance.

1. Build the frontend: `cd frontend && npm run build`
2. Start the demo server: `./tools/screenshots/serve_demo.sh` (port 8002; a throwaway
   copy in `tools/screenshots/.demo/` with no tokens and every job off)
3. In another terminal: `.venv/bin/python3 tools/screenshots/capture.py`
4. Look at the new PNGs, then stop the server with Ctrl-C.

`seed_demo.py` holds the fake week (events, goals, tasks, plan blocks, event
options, hours); `capture.py` picks the views. Needs Chrome or Chromium.
