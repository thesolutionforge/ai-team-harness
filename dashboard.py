"""
dashboard.py - a small local web page for the AI team harness.

Listens ONLY on 127.0.0.1, so nothing else on the network can reach it.
All the real work (running Claude, Codex and the merge) lives in conductor.py;
this file just serves the page and answers its requests.

    python dashboard.py          # start the server
    python dashboard.py --open   # start and open the page in your browser

Security notes (a web page you visit elsewhere can still talk to 127.0.0.1,
so loopback alone is not enough):
  - every /api request must carry a random token that only our page knows;
  - the Host and Origin headers must point at this server;
  - the page gets a Content-Security-Policy so only our own script runs.
"""

import json
import secrets
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import conductor

HOST = "127.0.0.1"          # loopback only: never reachable from other machines
PORT = 8765
HERE = Path(__file__).resolve().parent
PAGE = HERE / "dashboard.html"

# New random token each time the server starts. The page receives it in a
# <meta> tag and sends it back with every API call. Other sites never see it.
TOKEN = secrets.token_urlsafe(32)

# Only these addresses may appear in the Host / Origin headers.
ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}

# Progress of runs started from the page, keyed by run folder name.
# Each entry: {"task": str, "steps": {"claude": {...}, "gpt": {...}, "merge": {...}}, "done": bool}
LIVE = {}


def start_run(task, timeout):
    """Create the run folder, then run the pipeline in a background thread
    so the web server stays responsive while the AIs work."""
    run_dir = conductor.new_run_dir()
    run_id = run_dir.name
    LIVE[run_id] = {"task": task, "done": False,
                    "steps": {s: {"status": "waiting"} for s in ("claude", "gpt", "merge")}}

    def report(step, status):
        # Called by conductor as each step starts and finishes.
        entry = LIVE[run_id]["steps"][step]
        entry["status"] = status
        if status == "working":
            entry["started"] = time.time()
        else:
            entry["ended"] = time.time()

    def work():
        try:
            conductor.run_task(task, timeout, report=report, run_dir=run_dir)
        except Exception as e:                      # e.g. a CLI is missing
            for step in LIVE[run_id]["steps"].values():
                if step["status"] in ("waiting", "working"):
                    step["status"] = f"error: {e}"
        LIVE[run_id]["done"] = True

    threading.Thread(target=work, daemon=True).start()
    return run_id


def read_file(folder, name):
    """Return a file's text, or None if it does not exist yet."""
    path = folder / name
    return path.read_text(encoding="utf-8") if path.exists() else None


def list_runs():
    """Past runs, newest first, with the first line of the task as a title."""
    if not conductor.RUNS_DIR.exists():
        return []
    runs = []
    for folder in sorted(conductor.RUNS_DIR.iterdir(), reverse=True):
        task = read_file(folder, "task.md")
        if folder.is_dir() and task:
            runs.append({"id": folder.name, "task": task.strip().splitlines()[0],
                         "merged": (folder / "merged.md").exists()})
    return runs


def run_details(run_id):
    """Everything the page needs to show one run: files plus live progress."""
    folder = conductor.RUNS_DIR / run_id
    if not folder.is_dir():
        return None
    return {
        "id": run_id,
        "task": read_file(folder, "task.md"),
        "claude": read_file(folder, "claude.md"),
        "gpt": read_file(folder, "gpt.md"),
        "merged": read_file(folder, "merged.md"),
        "live": LIVE.get(run_id),      # None for runs from before this server started
    }


class Handler(BaseHTTPRequestHandler):
    """Routes: the page itself plus a few small JSON endpoints."""

    def send_json(self, data, code=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def request_allowed(self):
        """True only for requests from our own page. Checks, in order:
        the Host header, the Origin header (if the browser sent one),
        and the secret token for /api calls."""
        if self.headers.get("Host") not in ALLOWED_HOSTS:
            return False
        origin = self.headers.get("Origin")
        if origin is not None and origin not in ALLOWED_ORIGINS:
            return False
        if self.path.startswith("/api/") and self.headers.get("X-Auth-Token") != TOKEN:
            return False
        return True

    def send_page(self):
        """Serve dashboard.html with the token and a one-off nonce filled in.
        The nonce lets our inline <script> run while the CSP blocks any other."""
        nonce = secrets.token_urlsafe(16)
        html = PAGE.read_text(encoding="utf-8").replace("__TOKEN__", TOKEN).replace("__NONCE__", nonce)
        body = html.encode("utf-8")
        csp = (
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}' https://cdnjs.cloudflare.com; "
            "style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "connect-src 'self'; img-src 'self' data:; "
            "base-uri 'none'; form-action 'none'"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", csp)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.request_allowed():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/":
            self.send_page()
        elif self.path == "/api/status":
            self.send_json(conductor.check_auth())
        elif self.path == "/api/runs":
            self.send_json(list_runs())
        elif self.path.startswith("/api/runs/"):
            run_id = self.path.rsplit("/", 1)[1]
            if "/" in run_id or ".." in run_id:          # keep requests inside runs/
                return self.send_json({"error": "bad id"}, 400)
            details = run_details(run_id)
            self.send_json(details if details else {"error": "not found"}, 200 if details else 404)
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.request_allowed():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/api/run":
            # Insisting on JSON means a cross-site form post can never match.
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                return self.send_json({"error": "expected JSON"}, 415)
            length = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(length) or b"{}")
            task = (data.get("task") or "").strip()
            if not task:
                return self.send_json({"error": "Type a task first."}, 400)
            run_id = start_run(task, int(data.get("timeout") or 300))
            self.send_json({"id": run_id})
        else:
            self.send_json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass                            # keep the console quiet


def main():
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"AI team dashboard running at {url}  (Ctrl+C to stop)")
    if "--open" in sys.argv:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
