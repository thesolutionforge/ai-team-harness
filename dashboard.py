"""
dashboard.py - a small local web page for the AI team harness.

Listens ONLY on 127.0.0.1, so nothing else on the network can reach it.
All the real work (running Claude, Codex and the merge) lives in conductor.py;
this file just serves the page and answers its requests.

    python dashboard.py              # start the server on port 8765
    python dashboard.py --open       # start and open the page in your browser
    python dashboard.py --port 8766  # use another port (e.g. a second copy)

Security notes (a web page you visit elsewhere can still talk to 127.0.0.1,
so loopback alone is not enough):
  - every /api request must carry a random token that only our page knows;
  - the Host and Origin headers must point at this server;
  - the page gets a Content-Security-Policy so only our own script runs.
"""

import argparse
import json
import secrets
import socket
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
SETTINGS = HERE / "settings.json"   # remembers your last choices (roles, mode, rounds)

# New random token each time the server starts. The page receives it in a
# <meta> tag and sends it back with every API call. Other sites never see it.
TOKEN = secrets.token_urlsafe(32)

# Only these addresses may appear in the Host / Origin headers.
# (Filled in by set_port(), because the port can be changed with --port.)
ALLOWED_HOSTS = set()
ALLOWED_ORIGINS = set()


def set_port(port):
    """Choose the port and update the Host/Origin allow-lists to match."""
    global PORT, ALLOWED_HOSTS, ALLOWED_ORIGINS
    PORT = port
    ALLOWED_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
    ALLOWED_ORIGINS = {f"http://{h}" for h in ALLOWED_HOSTS}


set_port(PORT)

MAX_ROLE_TEXT = 2000        # characters allowed in a custom role description
MODES = ("together", "debate")

# Progress of runs started from the page, keyed by run folder name.
# Each entry: {"task": str, "steps": {"claude": {...}, "gpt": {...}, "merge": {...}}, "done": bool}
LIVE = {}


# ---------------------------------------------------------------------------
# Settings (your last choices) and input cleaning
# ---------------------------------------------------------------------------

def default_settings():
    return {"roles": {"claude": conductor.no_role(), "gpt": conductor.no_role()},
            "mode": "together", "critique_rounds": 1}


def load_settings():
    """Your remembered choices, with defaults filled in for anything missing."""
    settings = default_settings()
    try:
        settings.update(json.loads(SETTINGS.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(settings):
    SETTINGS.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def clean_role(raw, presets):
    """Turn whatever the page sent into a safe {"name", "text"} role.
    Preset names take their text from roles.json; "Custom" keeps the typed
    text (trimmed and capped); anything else becomes General."""
    if not isinstance(raw, dict):
        return conductor.no_role()
    name = str(raw.get("name", "General"))[:60]
    if name == "Custom":
        text = str(raw.get("text", "")).strip()[:MAX_ROLE_TEXT]
        return {"name": "Custom", "text": text} if text else conductor.no_role()
    if name in presets:
        return {"name": name, "text": presets[name]}
    return conductor.no_role()


def clean_mode(raw):
    return raw if raw in MODES else "together"


def clean_rounds(raw):
    return raw if raw in (1, 2) else 1


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------

def start_run(task, timeout, roles, mode, critique_rounds):
    """Create the run folder, then run the pipeline in a background thread
    so the web server stays responsive while the AIs work."""
    run_dir = conductor.new_run_dir()
    run_id = run_dir.name
    LIVE[run_id] = {"task": task, "done": False, "mode": mode,
                    "steps": {s: {"status": "waiting", "label": ""} for s in ("claude", "gpt", "merge")}}

    def report(step, status, label=""):
        # Called by conductor as each step starts and finishes a round.
        entry = LIVE[run_id]["steps"][step]
        entry["status"] = status
        entry["label"] = label
        if status == "working":
            entry["started"] = time.time()
            entry.pop("ended", None)
        else:
            entry["ended"] = time.time()

    def work():
        try:
            conductor.run_task(task, timeout, report=report, run_dir=run_dir,
                               roles=roles, mode=mode, critique_rounds=critique_rounds)
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


def read_info(folder):
    """Mode, rounds and roles saved with a run. Older runs only have
    roles-used.json (or nothing); fill in sensible defaults for them."""
    info = {"mode": "together", "critique_rounds": 0, "roles": None}
    for name in ("run-info.json", "roles-used.json"):
        text = read_file(folder, name)
        if text:
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if name == "run-info.json":
                info.update(data)
            else:
                info["roles"] = data
            break
    return info


def list_runs():
    """Past runs, newest first, with the first line of the task as a title."""
    if not conductor.RUNS_DIR.exists():
        return []
    runs = []
    for folder in sorted(conductor.RUNS_DIR.iterdir(), reverse=True):
        task = read_file(folder, "task.md")
        if folder.is_dir() and task:
            runs.append({"id": folder.name, "task": task.strip().splitlines()[0],
                         "mode": read_info(folder)["mode"]})
    return runs


def run_details(run_id):
    """Everything the page needs to show one run: files plus live progress."""
    folder = conductor.RUNS_DIR / run_id
    if not folder.is_dir():
        return None
    info = read_info(folder)
    return {
        "id": run_id,
        "task": read_file(folder, "task.md"),
        "mode": info["mode"],
        "critique_rounds": info["critique_rounds"],
        "roles": info["roles"],
        "claude": read_file(folder, "claude.md"),
        "gpt": read_file(folder, "gpt.md"),
        "merged": read_file(folder, "merged.md"),
        "debate": read_file(folder, "debate.md"),
        "live": LIVE.get(run_id),      # None for runs from before this server started
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class Server(ThreadingHTTPServer):
    """A server that insists on having its port to itself. Python's default
    lets a second copy bind the same port on Windows, and then the old copy
    keeps answering; this makes the second copy fail with a clear message."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):          # Windows only
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


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

    def read_json_body(self):
        """The POST body as a dict, or None if it is not JSON."""
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return None
        length = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
            return data if isinstance(data, dict) else None
        except ValueError:
            return None

    def do_GET(self):
        if not self.request_allowed():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/":
            self.send_page()
        elif self.path == "/api/status":
            self.send_json(conductor.check_auth())
        elif self.path == "/api/settings":
            # Role presets for the dropdowns plus your last choices.
            self.send_json({"presets": conductor.load_roles(), "last": load_settings()})
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
            data = self.read_json_body()
            if data is None:
                return self.send_json({"error": "expected JSON"}, 415)
            task = str(data.get("task") or "").strip()
            if not task:
                return self.send_json({"error": "Type a task first."}, 400)
            presets = conductor.load_roles()
            raw = data.get("roles") if isinstance(data.get("roles"), dict) else {}
            roles = {"claude": clean_role(raw.get("claude"), presets),
                     "gpt": clean_role(raw.get("gpt"), presets)}
            mode = clean_mode(data.get("mode"))
            rounds = clean_rounds(data.get("critique_rounds"))
            save_settings({"roles": roles, "mode": mode, "critique_rounds": rounds})   # remember for next time
            run_id = start_run(task, int(data.get("timeout") or 300), roles, mode, rounds)
            self.send_json({"id": run_id})
        else:
            self.send_json({"error": "not found"}, 404)

    def log_message(self, *args):
        pass                            # keep the console quiet


def main():
    parser = argparse.ArgumentParser(description="Local web page for the AI team harness.")
    parser.add_argument("--port", type=int, default=PORT, help=f"port to listen on (default {PORT})")
    parser.add_argument("--open", action="store_true", help="open the page in your browser")
    args = parser.parse_args()
    set_port(args.port)

    # First run on this computer: create the Codex settings folder, then say
    # who is signed in so a newcomer knows what to do next.
    if conductor.ensure_codex_home():
        print("First run: created .codex-home with the harness Codex settings.")
    for who, status in conductor.check_auth().items():
        print(f"{'Claude' if who == 'claude' else 'GPT':6} {status['detail']}")

    try:
        server = Server((HOST, PORT), Handler)
    except OSError:
        print(f"Port {PORT} is already in use. Another dashboard window is probably open:")
        print("close it first, or start this one with --port <another number>.")
        sys.exit(1)
    url = f"http://{HOST}:{PORT}/"
    print(f"AI team dashboard running at {url}  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
