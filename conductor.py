"""
conductor.py - the AI team harness engine.

Sends one task to Claude and to Codex at the same time, waits for both
(with a timeout), asks Claude to merge the two answers, and saves everything
under runs/<date-time>/.

Used two ways:
  - from the command line:  python conductor.py "some task"
  - imported by dashboard.py, which calls check_auth() and run_task().
"""

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Folder this script lives in. Everything else is relative to it.
HERE = Path(__file__).resolve().parent
RUNS_DIR = HERE / "runs"

# Codex gets its own settings folder so the desktop app's MCP servers,
# plugins and hooks never load. Codex reads this from the CODEX_HOME variable.
CODEX_HOME = HERE / ".codex-home"

# Where the Codex installer puts codex.exe. Used as a fallback when the
# program that launched us has an old PATH (e.g. an app opened before install).
EXTRA_TOOL_DIRS = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenAI" / "Codex" / "bin"]

# Extra instruction for Claude: this is a one-shot run, not an interactive
# session, so the "work in stages and wait for me" rule must not apply.
HARNESS_NOTE = (
    "This is an automated, non-interactive harness run. Answer the task "
    "completely in one reply. Do not work in stages or wait for the user; "
    "the stage-by-stage rule from the global CLAUDE.md does not apply here."
)

# Template for the merge request. The two answers are labelled A and B only,
# so the merger does not know which AI wrote which.
MERGE_PROMPT = """\
Two assistants were given the same task. Combine their answers.

## Task
{task}

## Answer A
{answer_a}

## Answer B
{answer_b}

## What to write
1. A heading "Final answer", then one answer to the task that combines the
   best ideas from A and B. Keep the task's format (e.g. if it asked for 3
   ideas, give 3).
2. A heading "Comparison", then three short bullet lists:
   - Agreed: points where A and B said the same thing.
   - Differed: points where they took different approaches.
   - Sources: for each idea in the final answer, whether it came from A, B,
     or both.
Write in plain Markdown. Do not guess or mention which AI wrote A or B.
"""

# Shared options for every subprocess we start.
PROC_OPTS = dict(cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace")


def find_tool(name):
    """Return the full path of a command-line tool, or raise a clear error."""
    search = os.pathsep.join([os.environ.get("PATH", "")] + [str(d) for d in EXTRA_TOOL_DIRS])
    path = shutil.which(name, path=search)
    if path is None:
        raise FileNotFoundError(f"Cannot find '{name}' on PATH. Open a new terminal or install it.")
    return path


def codex_env():
    """Environment for Codex: same as ours, plus its private settings folder."""
    return dict(os.environ, CODEX_HOME=str(CODEX_HOME))


def claude_command():
    """The common part of every Claude call.
    -p: print mode (answer once and exit).
    --tools "": no file or shell tools, Claude just writes.
    --strict-mcp-config: ignore MCP servers from the user's settings."""
    return [
        find_tool("claude"), "-p",
        "--tools", "",
        "--strict-mcp-config",
        "--append-system-prompt", HARNESS_NOTE,
    ]


# ---------------------------------------------------------------------------
# Sign-in checks (used by the dashboard cards)
# ---------------------------------------------------------------------------

def check_auth():
    """Ask each CLI whether it is signed in. Returns a dict like
    {"claude": {"ok": True, "detail": "..."}, "gpt": {"ok": False, "detail": "..."}}."""
    result = {}

    # Claude prints JSON with a loggedIn flag.
    try:
        out = subprocess.run([find_tool("claude"), "auth", "status"],
                             timeout=30, **PROC_OPTS).stdout
        info = json.loads(out)
        if info.get("loggedIn"):
            plan = info.get("subscriptionType", "")
            detail = f"Signed in as {info.get('email', 'unknown')}" + (f" ({plan} plan)" if plan else "")
            result["claude"] = {"ok": True, "detail": detail}
        else:
            result["claude"] = {"ok": False, "detail": "Not signed in. Run: claude auth login"}
    except Exception as e:  # tool missing, timeout, bad JSON...
        result["claude"] = {"ok": False, "detail": f"Check failed: {e}"}

    # Codex prints a sentence and exits 0 when signed in.
    try:
        proc = subprocess.run([find_tool("codex"), "login", "status"],
                              env=codex_env(), timeout=30, **PROC_OPTS)
        text = (proc.stdout + proc.stderr).strip().splitlines()[-1] if (proc.stdout + proc.stderr).strip() else ""
        if proc.returncode == 0 and "logged in" in text.lower() and "not logged" not in text.lower():
            result["gpt"] = {"ok": True, "detail": f"{text}. Model {codex_model()}"}
        else:
            result["gpt"] = {"ok": False, "detail": "Not signed in. Run: codex login"}
    except Exception as e:
        result["gpt"] = {"ok": False, "detail": f"Check failed: {e}"}

    return result


def codex_model():
    """Read the model and effort from the harness Codex config, for display only."""
    model, effort = "default", ""
    try:
        for line in (CODEX_HOME / "config.toml").read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "model":
                model = value.strip().strip('"')
            elif key.strip() == "model_reasoning_effort":
                effort = value.strip().strip('"')
    except OSError:
        pass
    return f"{model}, {effort} effort" if effort else model


# ---------------------------------------------------------------------------
# Running a task
# ---------------------------------------------------------------------------

def new_run_dir():
    """Create and return a fresh folder like runs/2026-10-23_14-05-09/."""
    run_dir = RUNS_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True)
    return run_dir


def wait_for(name, process, deadline, report, send=None):
    """Wait for a process until the deadline. `send` is optional text for its
    stdin (used for the long merge prompt). Returns (stdout, stderr, status)."""
    report(name, "working")
    remaining = max(1, deadline - time.monotonic())
    try:
        out, err = process.communicate(input=send, timeout=remaining)
        status = "ok" if process.returncode == 0 else f"exit code {process.returncode}"
    except subprocess.TimeoutExpired:
        process.kill()                 # stop the slow one so the run can finish
        out, err = process.communicate()
        status = "TIMED OUT"
    report(name, status)
    return out, err, status


def ask_claude(task, run_dir, deadline, report):
    """Round 1, Claude: answer comes back on stdout, saved to claude.md.
    The task goes in through stdin, so a task starting with "-" is never
    mistaken for a command-line flag."""
    process = subprocess.Popen(claude_command(), stdin=subprocess.PIPE, **PROC_OPTS)
    out, err, status = wait_for("claude", process, deadline, report, send=task)
    answer = out.strip() or f"(no answer: {status})"
    (run_dir / "claude.md").write_text(answer + "\n", encoding="utf-8")
    (run_dir / "claude.log").write_text(err, encoding="utf-8")
    return answer, status


def ask_codex(task, run_dir, deadline, report):
    """Round 1, Codex: stdout is noisy, so it writes its final answer to gpt.md
    (-o). --ephemeral means it does not save a resumable session."""
    gpt_file = run_dir / "gpt.md"
    cmd = [find_tool("codex"), "exec", "--ephemeral", "-o", str(gpt_file)]   # task comes via stdin
    process = subprocess.Popen(cmd, env=codex_env(), stdin=subprocess.PIPE, **PROC_OPTS)
    out, err, status = wait_for("gpt", process, deadline, report, send=task)
    if gpt_file.exists() and gpt_file.read_text(encoding="utf-8").strip():
        answer = gpt_file.read_text(encoding="utf-8").strip()
    else:
        answer = f"(no answer: {status})"
        gpt_file.write_text(answer + "\n", encoding="utf-8")
    (run_dir / "codex.log").write_text(out + "\n" + err, encoding="utf-8")
    return answer, status


def merge_answers(task, claude_answer, gpt_answer, run_dir, timeout, report):
    """Round 2: ask Claude to combine the two answers. Which one is A and which
    is B is chosen at random each run, so neither AI always sits first.
    The mapping is appended to merged.md AFTER merging; the merger never sees it."""
    if random.random() < 0.5:
        a, b, mapping = claude_answer, gpt_answer, "Answer A = Claude, Answer B = GPT"
    else:
        a, b, mapping = gpt_answer, claude_answer, "Answer A = GPT, Answer B = Claude"
    prompt = MERGE_PROMPT.format(task=task, answer_a=a, answer_b=b)

    # The prompt is long, so it goes in through stdin instead of the command line.
    process = subprocess.Popen(claude_command(), stdin=subprocess.PIPE, **PROC_OPTS)
    deadline = time.monotonic() + timeout
    out, err, status = wait_for("merge", process, deadline, report, send=prompt)
    merged = (out.strip() or f"(no merged answer: {status})") + f"\n\n---\n*{mapping}*\n"
    (run_dir / "merged.md").write_text(merged, encoding="utf-8")
    (run_dir / "merge.log").write_text(err, encoding="utf-8")
    return merged, status


def run_task(task, timeout=300, report=None, run_dir=None):
    """The whole pipeline. `report(step, status)` is called as things progress
    (step is "claude", "gpt" or "merge"). Returns the run folder."""
    report = report or (lambda step, status: None)
    run_dir = run_dir or new_run_dir()
    (run_dir / "task.md").write_text(task + "\n", encoding="utf-8")

    # Round 1: both AIs at once. Each runs in its own thread so the slow one
    # never blocks the fast one, and both share the same deadline.
    deadline = time.monotonic() + timeout
    results = {}
    threads = [
        threading.Thread(target=lambda: results.update(claude=ask_claude(task, run_dir, deadline, report))),
        threading.Thread(target=lambda: results.update(gpt=ask_codex(task, run_dir, deadline, report))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    claude_answer, claude_status = results["claude"]
    gpt_answer, gpt_status = results["gpt"]

    # Round 2: merge, but only if both answers actually arrived.
    if claude_status == "ok" and gpt_status == "ok":
        merge_answers(task, claude_answer, gpt_answer, run_dir, timeout, report)
    else:
        (run_dir / "merged.md").write_text(
            "(merge skipped: one of the answers failed or timed out)\n", encoding="utf-8")
        report("merge", "skipped")
    return run_dir


# ---------------------------------------------------------------------------
# Command-line use
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Send one task to Claude and Codex, then merge.")
    parser.add_argument("task", help="The task to send to both AIs")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait for each step (default 300)")
    args = parser.parse_args()

    # Windows consoles may not be UTF-8; make sure accents and quotes print fine.
    sys.stdout.reconfigure(encoding="utf-8")

    print("Starting Claude and Codex...")
    run_dir = run_task(args.task, args.timeout, report=lambda step, status: print(f"  {step}: {status}"))
    print("\n=== MERGED ===\n" + (run_dir / "merged.md").read_text(encoding="utf-8"))
    print(f"Saved to {run_dir}")


if __name__ == "__main__":
    main()
