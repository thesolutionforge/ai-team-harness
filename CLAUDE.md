# AI team harness

This folder is an automated harness: a Python script sends one task to Claude
and to Codex at the same time, then saves both answers.

## Rules for harness runs

- Answer the task completely in a single reply.
- Do not split the work into stages and do not stop to wait for the user.
  The stage-by-stage rule in the global CLAUDE.md is for interactive sessions
  and does NOT apply here.
- Reply with the answer only, in plain Markdown. No preamble, no questions.
