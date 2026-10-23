"""
conductor.py - send one task to Claude and to Codex at the same time,
wait for both (with a timeout), ask Claude to merge the two answers,
and save everything under runs/<date-time>/.

Usage (PowerShell):
    python conductor.py "Give 3 ideas for a cafe website homepage headline"
    python conductor.py --timeout 120 "Some task"
"""

import argparse
import os
import random
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


def find_tool(name):
    """Return the full path of a command-line tool, or exit with a clear message."""
    path = shutil.which(name)
    if path is None:
        sys.exit(f"Cannot find '{name}' on PATH. Open a new terminal or install it.")
    return path


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


def start_claude(task):
    """Launch Claude Code with the task on the command line."""
    return subprocess.Popen(
        claude_command() + [task], cwd=HERE,
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


def wait_for(name, process, deadline, send=None):
    """Wait for a process until the deadline. `send` is optional text for its
    stdin (used for the long merge prompt). Returns (stdout, stderr, status)."""
    remaining = max(1, deadline - time.monotonic())
    try:
        out, err = process.communicate(input=send, timeout=remaining)
        status = "ok" if process.returncode == 0 else f"exit code {process.returncode}"
    except subprocess.TimeoutExpired:
        process.kill()                 # stop the slow one so the run can finish
        out, err = process.communicate()
        status = "TIMED OUT"
    print(f"  {name}: {status}")
    return out, err, status


def merge_answers(task, claude_answer, gpt_answer, timeout):
    """Third request: ask Claude to combine the two answers.
    Which answer is A and which is B is chosen at random each run, so
    neither AI always sits in the first position. Returns (text, mapping, log)."""
    if random.random() < 0.5:
        a, b, mapping = claude_answer, gpt_answer, "Answer A = Claude, Answer B = GPT"
    else:
        a, b, mapping = gpt_answer, claude_answer, "Answer A = GPT, Answer B = Claude"

    prompt = MERGE_PROMPT.format(task=task, answer_a=a, answer_b=b)

    # The prompt is long, so it goes in through stdin instead of the command line.
    process = subprocess.Popen(
        claude_command(), cwd=HERE,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    deadline = time.monotonic() + timeout
    out, err, status = wait_for("merge", process, deadline, send=prompt)
    text = out.strip() or f"(no merged answer: {status})"
    return text, mapping, err


def main():
    parser = argparse.ArgumentParser(description="Send one task to Claude and Codex, then merge.")
    parser.add_argument("task", help="The task to send to both AIs")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait for each step (default 300)")
    args = parser.parse_args()

    # Windows consoles may not be UTF-8; make sure accents and quotes print fine.
    sys.stdout.reconfigure(encoding="utf-8")

    # One folder per run, named by date and time, e.g. runs/2026-10-23_14-05-09/
    run_dir = HERE / "runs" / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True)
    (run_dir / "task.md").write_text(args.task + "\n", encoding="utf-8")
    gpt_file = run_dir / "gpt.md"

    # --- Step 1: both AIs answer the task in parallel -----------------------
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

    # --- Step 2: merge --------------------------------------------------------
    both_ok = claude_status == "ok" and codex_status == "ok"
    if both_ok:
        print("Merging...")
        merged, mapping, merge_err = merge_answers(
            args.task, claude_answer, gpt_answer, args.timeout)
        # The mapping is appended AFTER merging, so the merger never saw it.
        merged_file_text = f"{merged}\n\n---\n*{mapping}*\n"
        (run_dir / "merge.log").write_text(merge_err, encoding="utf-8")
    else:
        merged = "(merge skipped: one of the answers failed or timed out)"
        merged_file_text = merged + "\n"
    (run_dir / "merged.md").write_text(merged_file_text, encoding="utf-8")

    print("\n=== MERGED ===\n" + merged_file_text)
    print(f"Saved to {run_dir}")


if __name__ == "__main__":
    main()
