"""
conductor.py - the AI team harness engine.

Two modes:
  together  Round 1: Claude and Codex answer independently. Then Claude
            merges the two answers.
  debate    Round 1 as above. Then one or two critique rounds: each AI reads
            the other's answer (labelled only by role), critiques it and
            revises its own. Then Claude merges the final answers plus the
            critiques.

Everything is saved under runs/<date-time>/ with one file per round.
Each AI can be given a role (see roles.json). The role text is put at the
start of every prompt that AI receives.

Used two ways:
  - command line:  python conductor.py "task" --mode debate --claude-role Strategist
  - imported by dashboard.py, which calls check_auth(), load_roles() and run_task().
"""

import argparse
import json
import os
import random
import re
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
ROLES_FILE = HERE / "roles.json"

# Codex gets its own settings folder so the desktop app's MCP servers,
# plugins and hooks never load. Codex reads this from the CODEX_HOME variable.
CODEX_HOME = HERE / ".codex-home"

# Written into a fresh .codex-home the first time the harness runs on a computer.
CODEX_CONFIG = """\
# Codex settings used ONLY by the AI team harness.
# Kept separate from the Codex desktop app's config (~/.codex) via the CODEX_HOME
# environment variable, so none of the app's MCP servers, plugins or hooks load here.
# No mcp_servers, no plugins: Codex just answers the task.

# Model and thinking depth. Change these if your ChatGPT plan offers different models.
model = "gpt-6-astra"
model_reasoning_effort = "high"
"""

# What to tell someone who is not signed in yet.
CLAUDE_SIGNIN_HELP = "Not signed in. In a terminal, run: claude auth login"
CODEX_SIGNIN_HELP = "Not signed in. Double-click Sign-In-Codex.cmd and sign in with your ChatGPT account."

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

# ---- Prompt templates ------------------------------------------------------
# Answers are labelled A and B only, so the merger never knows which AI wrote
# which. {label_a} / {label_b} carry the role, e.g. "Answer A (role: Critic)".

MERGE_PROMPT = """\
Two assistants were given the same task. Combine their answers.
Each answer notes the role its author was asked to play, if any.

## Task
{task}

## {label_a}
{answer_a}

## {label_b}
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

CRITIQUE_PROMPT = """\
You answered a task earlier. Another assistant answered the same task; its
answer is below, labelled only with the role it was asked to play.

## Task
{task}

## Your earlier answer
{own}

## The other answer{other_tag}
{other}

## What to write
1. A heading "Critique". Point out errors, weak reasoning and claims that
   need checking in the other answer. Then say honestly what the other answer
   does better than yours.
2. A heading "Revised answer". Rewrite your own answer, improving it with
   anything the other answer got right and fixing anything it exposed in
   yours. Keep the task's format. This must be a complete, standalone answer.
Write in plain Markdown. Do not guess or mention who wrote the other answer.
"""

DEBATE_MERGE_PROMPT = """\
Two assistants answered the same task, then each read the other's answer,
critiqued it and revised its own. Below are their final revised answers and
their critiques, labelled A and B. Each notes the author's role, if any.

## Task
{task}

## Final {label_a}
{answer_a}

## Final {label_b}
{answer_b}

## A's critique of B's answer
{critique_a}

## B's critique of A's answer
{critique_b}

## What to write
1. A heading "Final answer": one answer to the task that takes the best of
   A and B. Keep the task's format. If the task asks for a recommendation,
   give one clearly.
2. A heading "What the debate changed": short bullets on what each side
   conceded or improved after reading the other.
3. A heading "Remaining disagreements": points where A and B still differ.
4. A heading "Claims to verify": facts, figures or prices in the answers that
   should be checked before relying on them.
Write in plain Markdown. Do not guess or mention which AI wrote A or B.
"""

# Shared options for every subprocess we start.
PROC_OPTS = dict(cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 text=True, encoding="utf-8", errors="replace")

OTHER = {"claude": "gpt", "gpt": "claude"}      # who critiques whom
DISPLAY = {"claude": "Claude", "gpt": "GPT"}


def find_tool(name):
    """Return the full path of a command-line tool, or raise a clear error."""
    search = os.pathsep.join([os.environ.get("PATH", "")] + [str(d) for d in EXTRA_TOOL_DIRS])
    path = shutil.which(name, path=search)
    if path is None:
        raise FileNotFoundError(f"Cannot find '{name}' on PATH. Open a new terminal or install it.")
    return path


def ensure_codex_home():
    """Create .codex-home with the harness settings if it is missing (first run
    on a new computer). Returns True if it was created just now."""
    if (CODEX_HOME / "config.toml").exists():
        return False
    CODEX_HOME.mkdir(exist_ok=True)
    (CODEX_HOME / "config.toml").write_text(CODEX_CONFIG, encoding="utf-8")
    return True


def codex_env():
    """Environment for Codex: same as ours, plus its private settings folder."""
    ensure_codex_home()
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
# Roles
# ---------------------------------------------------------------------------

def load_roles():
    """Preset roles from roles.json as {name: instruction}. Keys starting
    with "_" are comments for humans and are skipped."""
    data = json.loads(ROLES_FILE.read_text(encoding="utf-8"))
    return {name: text for name, text in data.items() if not name.startswith("_")}


def no_role():
    """The default: no role, so the prompt is just the task."""
    return {"name": "General", "text": ""}


def role_from_name(name):
    """Look a preset up by name; unknown names fall back to General."""
    return {"name": name, "text": load_roles().get(name, "")}


def with_role(prompt, role):
    """Put the role instruction (if any) at the start of a prompt."""
    text = (role or {}).get("text", "").strip()
    return f"Your role: {text}\n\n{prompt}" if text else prompt


def role_label(role):
    """Short tag for prompts and the merged.md footer, e.g. ' (role: Critic)'.
    Empty for General so the prompt reads exactly as before."""
    name = (role or {}).get("name", "General")
    return f" (role: {name})" if (role or {}).get("text", "").strip() else ""


def heading_for(who, roles):
    """Human-readable heading for debate.md, e.g. 'Claude (Strategist)'."""
    name = roles[who]["name"]
    return f"{DISPLAY[who]} ({name})" if roles[who]["text"].strip() else DISPLAY[who]


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
            result["claude"] = {"ok": False, "detail": CLAUDE_SIGNIN_HELP}
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
            result["gpt"] = {"ok": False, "detail": CODEX_SIGNIN_HELP}
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
# Talking to the two CLIs
# ---------------------------------------------------------------------------

def new_run_dir():
    """Create and return a fresh folder like runs/2026-10-23_14-05-09/."""
    run_dir = RUNS_DIR / datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir.mkdir(parents=True)
    return run_dir


def wait_for(name, process, deadline, report, send, label=""):
    """Wait for a process until the deadline. `send` is the text for its stdin
    (the prompt). `label` names the round for the live display.
    Returns (stdout, stderr, status)."""
    report(name, "working", label)
    remaining = max(1, deadline - time.monotonic())
    try:
        out, err = process.communicate(input=send, timeout=remaining)
        status = "ok" if process.returncode == 0 else f"exit code {process.returncode}"
    except subprocess.TimeoutExpired:
        process.kill()                 # stop the slow one so the run can finish
        out, err = process.communicate()
        status = "TIMED OUT"
    report(name, status, label)
    return out, err, status


def ask_claude(prompt, out_file, deadline, report, label=""):
    """Claude: the answer comes back on stdout and is saved to out_file.
    The prompt goes in through stdin, so text starting with "-" is never
    mistaken for a command-line flag."""
    process = subprocess.Popen(claude_command(), stdin=subprocess.PIPE, **PROC_OPTS)
    out, err, status = wait_for("claude", process, deadline, report, prompt, label)
    answer = out.strip() or f"(no answer: {status})"
    out_file.write_text(answer + "\n", encoding="utf-8")
    out_file.with_suffix(".log").write_text(err, encoding="utf-8")
    return answer, status


def ask_codex(prompt, out_file, deadline, report, label=""):
    """Codex: stdout is noisy, so it writes its final answer to out_file (-o).
    --ephemeral means it does not save a resumable session."""
    cmd = [find_tool("codex"), "exec", "--ephemeral", "-o", str(out_file)]   # prompt comes via stdin
    process = subprocess.Popen(cmd, env=codex_env(), stdin=subprocess.PIPE, **PROC_OPTS)
    out, err, status = wait_for("gpt", process, deadline, report, prompt, label)
    if out_file.exists() and out_file.read_text(encoding="utf-8").strip():
        answer = out_file.read_text(encoding="utf-8").strip()
    else:
        answer = f"(no answer: {status})"
        out_file.write_text(answer + "\n", encoding="utf-8")
    out_file.with_suffix(".log").write_text(out + "\n" + err, encoding="utf-8")
    return answer, status


def ask_both(prompts, run_dir, prefix, label, timeout, report):
    """Send one prompt to each AI at the same time. Files are named
    <prefix>-claude.md and <prefix>-gpt.md. Each runs in its own thread so the
    slow one never blocks the fast one, and both share the same deadline.
    Returns {"claude": (answer, status), "gpt": (answer, status)}."""
    deadline = time.monotonic() + timeout
    results = {}
    threads = [
        threading.Thread(target=lambda: results.update(claude=ask_claude(
            prompts["claude"], run_dir / f"{prefix}-claude.md", deadline, report, label))),
        threading.Thread(target=lambda: results.update(gpt=ask_codex(
            prompts["gpt"], run_dir / f"{prefix}-gpt.md", deadline, report, label))),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def ask_merger(prompt, run_dir, timeout, report, mapping):
    """Claude combines the answers. The real A/B mapping is appended to
    merged.md AFTER merging, so the merger never sees it."""
    process = subprocess.Popen(claude_command(), stdin=subprocess.PIPE, **PROC_OPTS)
    deadline = time.monotonic() + timeout
    out, err, status = wait_for("merge", process, deadline, report, prompt)
    merged = (out.strip() or f"(no merged answer: {status})") + f"\n\n---\n*{mapping}*\n"
    (run_dir / "merged.md").write_text(merged, encoding="utf-8")
    (run_dir / "merge.log").write_text(err, encoding="utf-8")
    return merged, status


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

def shuffle_ab(roles):
    """Decide at random which AI is "A" and which is "B" for the merger, so
    neither always sits first. Returns (order, mapping_text)."""
    order = ["claude", "gpt"] if random.random() < 0.5 else ["gpt", "claude"]
    mapping = (f"Answer A = {DISPLAY[order[0]]}{role_label(roles[order[0]])}, "
               f"Answer B = {DISPLAY[order[1]]}{role_label(roles[order[1]])}")
    return order, mapping


def split_critique(text):
    """Split a critique-round reply into (critique, revised answer) at the
    "Revised answer" heading. If the heading is missing, both halves are the
    whole text, so nothing is lost."""
    match = re.search(r"^#{1,3}\s*\**\s*revised answer\b.*$", text, flags=re.IGNORECASE | re.MULTILINE)
    if not match:
        return text.strip(), text.strip()
    return text[:match.start()].strip(), text[match.end():].strip()


def run_task(task, timeout=300, report=None, run_dir=None, roles=None,
             mode="together", critique_rounds=1):
    """The whole pipeline. `report(step, status, label)` is called as things
    progress (step is "claude", "gpt" or "merge"; label names the round).
    `roles` is {"claude": role, "gpt": role}; missing means General.
    Returns the run folder."""
    report = report or (lambda step, status, label="": None)
    run_dir = run_dir or new_run_dir()
    roles = {who: (roles or {}).get(who) or no_role() for who in ("claude", "gpt")}
    critique_rounds = critique_rounds if mode == "debate" else 0

    (run_dir / "task.md").write_text(task + "\n", encoding="utf-8")
    (run_dir / "run-info.json").write_text(json.dumps(
        {"mode": mode, "critique_rounds": critique_rounds, "roles": roles}, indent=2), encoding="utf-8")

    # ---- Round 1: both answer the task independently -----------------------
    prompts = {who: with_role(task, roles[who]) for who in roles}
    results = ask_both(prompts, run_dir, "round1", "Round 1", timeout, report)
    answers = {who: results[who][0] for who in roles}
    all_ok = all(results[who][1] == "ok" for who in roles)

    # debate.md collects every round in order, for the Debate tab.
    debate = [f"# Round 1\n"] + [f"## {heading_for(who, roles)}\n\n{answers[who]}\n" for who in roles]
    critiques = {}

    # ---- Critique rounds (debate mode only) --------------------------------
    for n in range(1, critique_rounds + 1):
        if not all_ok:
            break
        prompts = {}
        for who in roles:
            other = OTHER[who]
            body = CRITIQUE_PROMPT.format(task=task, own=answers[who], other=answers[other],
                                          other_tag=role_label(roles[other]))
            prompts[who] = with_role(body, roles[who])
        results = ask_both(prompts, run_dir, f"round{n + 1}", f"Critique {n}", timeout, report)
        all_ok = all(results[who][1] == "ok" for who in roles)
        debate.append(f"# Critique round {n}\n")
        for who in roles:
            critique, revised = split_critique(results[who][0])
            (run_dir / f"round{n + 1}-{who}-critique.md").write_text(critique + "\n", encoding="utf-8")
            (run_dir / f"round{n + 1}-{who}-revised.md").write_text(revised + "\n", encoding="utf-8")
            critiques[who] = critique
            if results[who][1] == "ok":
                answers[who] = revised          # the revised answer carries forward
            debate.append(f"## {heading_for(who, roles)} on {DISPLAY[OTHER[who]]}'s answer\n\n"
                          f"### Critique\n\n{critique}\n\n### Revised answer\n\n{revised}\n")

    # Final answers (after any revisions) are what the tabs show.
    (run_dir / "claude.md").write_text(answers["claude"] + "\n", encoding="utf-8")
    (run_dir / "gpt.md").write_text(answers["gpt"] + "\n", encoding="utf-8")
    if mode == "debate":
        (run_dir / "debate.md").write_text("\n".join(debate), encoding="utf-8")

    # ---- Merge -------------------------------------------------------------
    if not all_ok:
        (run_dir / "merged.md").write_text(
            "(merge skipped: one of the answers failed or timed out)\n", encoding="utf-8")
        report("merge", "skipped", "")
        return run_dir

    order, mapping = shuffle_ab(roles)
    a, b = order
    if critiques:
        prompt = DEBATE_MERGE_PROMPT.format(
            task=task,
            label_a="Answer A" + role_label(roles[a]), answer_a=answers[a],
            label_b="Answer B" + role_label(roles[b]), answer_b=answers[b],
            critique_a=critiques[a], critique_b=critiques[b])
    else:
        prompt = MERGE_PROMPT.format(
            task=task,
            label_a="Answer A" + role_label(roles[a]), answer_a=answers[a],
            label_b="Answer B" + role_label(roles[b]), answer_b=answers[b])
    ask_merger(prompt, run_dir, timeout, report, mapping)
    return run_dir


# ---------------------------------------------------------------------------
# Command-line use
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Send one task to Claude and Codex, then merge.")
    parser.add_argument("task", help="The task to send to both AIs")
    parser.add_argument("--timeout", type=int, default=300,
                        help="Seconds to wait for each step (default 300)")
    parser.add_argument("--claude-role", default="General", help="Preset role name from roles.json")
    parser.add_argument("--gpt-role", default="General", help="Preset role name from roles.json")
    parser.add_argument("--mode", choices=["together", "debate"], default="together")
    parser.add_argument("--rounds", type=int, choices=[1, 2], default=1,
                        help="Critique rounds in debate mode (default 1)")
    args = parser.parse_args()

    # Windows consoles may not be UTF-8; make sure accents and quotes print fine.
    sys.stdout.reconfigure(encoding="utf-8")

    # Stop early with clear instructions if either AI is not signed in.
    status = check_auth()
    problems = [f"{who}: {s['detail']}" for who, s in status.items() if not s["ok"]]
    if problems:
        sys.exit("\n".join(problems))

    roles = {"claude": role_from_name(args.claude_role), "gpt": role_from_name(args.gpt_role)}
    print(f"Mode: {args.mode}. Claude as {roles['claude']['name']}, Codex as {roles['gpt']['name']}.")
    run_dir = run_task(args.task, args.timeout, roles=roles, mode=args.mode, critique_rounds=args.rounds,
                       report=lambda step, status, label="": print(f"  {step}: {label + ' ' if label else ''}{status}"))
    print("\n=== MERGED ===\n" + (run_dir / "merged.md").read_text(encoding="utf-8"))
    print(f"Saved to {run_dir}")


if __name__ == "__main__":
    main()
