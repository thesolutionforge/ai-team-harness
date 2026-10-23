# AI team harness

This folder is an automated harness: a Python script sends one task to Claude
and to Codex at the same time, then saves both answers.

## Rules for harness runs

- Answer the task completely in a single reply.
- Do not split the work into stages and do not stop to wait for the user.
  The stage-by-stage rule in the global CLAUDE.md is for interactive sessions
  and does NOT apply here.
- Reply with the answer only, in plain Markdown. No preamble, no questions.

## Rules for working on this repository (interactive sessions)

- This repository is PUBLIC on GitHub. Before every `git push`, scan the
  changes being pushed (every file in every commit not yet on the remote) for
  passwords, tokens, API keys, login or credential files (for example
  auth.json) and personal e-mail addresses. If anything is found, do not push:
  stop and report it instead.
- `.codex-home/`, `settings.json` and `runs/` must stay ignored by git.
