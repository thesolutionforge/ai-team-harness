"""
conductor.py - send one task to Claude and to Codex at the same time,
wait for both (with a timeout), and save everything under runs/<date-time>/.

Usage (PowerShell):
    python conductor.py "Give 3 ideas for a cafe website homepage headline"
    python conductor.py --timeout 120 "Some task"
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# Folder this script lives in. Everything else is relative to it.
HERE = Path(__file__).resolve().parent

# Codex gets its own settings folder so the desktop app's MCP servers,
# plugins and hooks never load. Codex reads this from the CODEX_HOME variable.
CODEX_HOME = HERE / ".codex-home"

# Extra instruction for Claude: this is a one-shot run, not an interactive
# session, so the "work in stages and wait for me" rule must not apply.
HARNESS_NOTE = (
    "This is an automated, non-interactive harness run. Answer the task "
    "completely in one reply. Do not work in stages or wait for the user; "
    "the stage-by-stage rule from the global CLAUDE.md does not apply here."
)


def find_tool(name):
    """Return the full path of a command-line tool, or exit with a clear message."""
    path = shutil.which(name)
    if path is None:
        sys.exit(f"Cannot find '{name}' on PATH. Open a new terminal or install it.")
    return path


def start_claude(task):
    """Launch Claude Code in print mode (-p). Its answer comes back on stdout.
    --tools "" removes file/shell tools, so Claude just answers the question."""
    cmd = [
        find_tool("claude"), "-p",
        "--tools", "",
        "--strict-mcp-config",       # ignore MCP servers from user settings
        "--append-system-prompt", HARNESS_NOTE,
        task,
    ]
    return subprocess.Popen(
        cmd, cwd=HERE,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


def start_codex(task, answer_file):
    """Launch Codex in exec mode. Its stdout is noisy, so we ask it to write
    the final answer to a file (-o) and read that instead.
    --ephemeral means Codex does not save this as a resumable session."""
    env = dict(os.environ, CODEX_HOME=str(CODEX_HOME))
    cmd = [
        find_tool("codex"), "exec",
        "--ephemeral",
        "-o", str(answer_file),
        task,
    ]
    return subprocess.Popen(
        cmd, cwd=HERE, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


def wait_for(name, process, deadline):
    """Wait for a process until the shared deadline. Returns (stdout, stderr, status)."""
    remaining = max(1, deadline - time.monotonic())
    try:
        out, err = process.communicate(timeout=remaining)
        status = "ok" if process.returncode == 0 else f"exit code {process.returncode}"
    except subprocess.TimeoutExpired:
        process.kill()                 # stop the slow one so the run can finish
        out, err = process.communicate()
        status = "TIMED OUT"
    print(f"  {name}: {status}")
    return out, err, status


def main():
    parser = argparse.ArgumentParser(description="Send one task to Claude and Codex.")
    parser.add_argument("task", help="The task to send to both AIs")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait for each AI (default 300)")
    args = parser.parse_args()

    # Windows consoles may not be UTF-8; make sure accents and quotes print fine.
    sys.stdout.reconfigure(encoding="utf-8")

    # One folder per run, named by date and time, e.g. runs/2026-10-23_14-05-09/
    run_dir = HERE / "runs" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True)
    (run_dir / "task.md").write_text(args.task + "\n", encoding="utf-8")
    gpt_file = run_dir / "gpt.md"

    print(f"Run folder: {run_dir}")
    print("Starting Claude and Codex...")
    claude = start_claude(args.task)
    codex = start_codex(args.task, gpt_file)

    # Both are already running; now wait for each against the same deadline.
    deadline = time.monotonic() + args.timeout
    claude_out, claude_err, claude_status = wait_for("claude", claude, deadline)
    codex_out, codex_err, codex_status = wait_for("codex", codex, deadline)

    # Claude's answer is its stdout.
    claude_answer = claude_out.strip() or f"(no answer: {claude_status})"
    (run_dir / "claude.md").write_text(claude_answer + "\n", encoding="utf-8")

    # Codex's answer is the file it wrote; if it never got there, say so.
    if gpt_file.exists() and gpt_file.read_text(encoding="utf-8").strip():
        gpt_answer = gpt_file.read_text(encoding="utf-8").strip()
    else:
        gpt_answer = f"(no answer: {codex_status})"
        gpt_file.write_text(gpt_answer + "\n", encoding="utf-8")

    # Keep the raw logs too; handy when something goes wrong.
    (run_dir / "claude.log").write_text(claude_err, encoding="utf-8")
    (run_dir / "codex.log").write_text(codex_out + "\n" + codex_err, encoding="utf-8")

    print("\n=== CLAUDE ===\n" + claude_answer)
    print("\n=== GPT (Codex) ===\n" + gpt_answer)
    print(f"\nSaved to {run_dir}")


if __name__ == "__main__":
    main()
