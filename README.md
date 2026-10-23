# AI team harness

A small local tool that gives one task to two AI coding assistants at once,
**Claude** (Anthropic) and **GPT** (OpenAI, via the Codex CLI), then has Claude
merge the two answers into one. You can give each AI a role, or run a
**Debate** where each critiques the other before the merge.

Everything runs on your own computer through the two official command-line
tools, signed in to **your own** Claude and ChatGPT accounts. No API keys are
needed and nothing is sent anywhere except to Anthropic and OpenAI through
their own tools.

## What you need

- Windows 10 or 11. (The Python code also runs on macOS and Linux, but the
  double-click launchers are Windows only and that path is untested.)
- [Python 3.12 or newer](https://www.python.org/downloads/), with "Add python
  to PATH" ticked during install.
- A Claude subscription (Pro or Max) and a ChatGPT subscription (Plus or
  higher). Runs use the allowance of those plans, see below.
- The two command-line tools, installed with their official installers:

  **Claude Code** (open PowerShell and run):

  ```powershell
  irm https://claude.ai/install.ps1 | iex
  ```

  **Codex CLI** (same PowerShell window):

  ```powershell
  irm https://chatgpt.com/codex/install.ps1 | iex
  ```

  Then close PowerShell and open a new one so the tools are on your PATH.
  Check with `claude --version` and `codex --version`. If an installer
  command has changed, follow the official docs for
  [Claude Code](https://docs.anthropic.com/en/docs/claude-code) and
  [Codex](https://developers.openai.com/codex/cli).

## Set up

1. Download or clone this folder anywhere on your computer.

2. **Sign in to Claude.** In PowerShell, run:

   ```powershell
   claude auth login
   ```

   A browser window opens; sign in with your Claude account.

3. **Sign in to Codex (GPT).** Double-click **`Sign-In-Codex.cmd`** in this
   folder. A browser window opens; sign in with your ChatGPT account.

   This login is stored in a `.codex-home` folder inside the project, created
   automatically on first run. It is kept separate from the Codex desktop
   app's own settings so none of that app's plugins or connectors load here.
   The folder is ignored by git, so it is never committed.

4. **Launch.** Double-click **`Start-Harness.cmd`**. A black window stays open
   while the dashboard runs, and your browser opens at
   <http://127.0.0.1:8765>. Close the window to stop.

   The window also prints whether each AI is signed in. The two cards at the
   top of the page show the same thing.

## Using it

- **Role** (on each card): General sends the task as is. The presets
  (Strategist, Creative partner, Critic, Researcher, Technical expert) add a
  short instruction in front of the task; Custom lets you type your own. The
  texts live in `roles.json`, edit them freely.
- **Mode:** *Work together* has both answer, then Claude merges. *Debate*
  adds one or two critique rounds where each AI reads the other's answer
  (labelled only by role, not by name), points out weaknesses and revises its
  own; the merger then also lists what the debate changed, remaining
  disagreements and claims to verify.
- **Tabs:** Claude's answer, GPT's answer, the Debate (rounds in order) and
  the Merged result. **Recent runs** in the sidebar reopens past results.
- Every run is saved under `runs/<date-time>/` as plain Markdown files.
- The merger never knows which AI wrote which answer; the real mapping is
  appended to `merged.md` afterwards.

From the command line, without the dashboard:

```powershell
python conductor.py "Your task here" --claude-role Strategist --gpt-role Critic --mode debate
```

## Plan allowance

Each run sends real requests through your Claude and ChatGPT subscriptions:
two answers plus one merge for Work together, and two more requests per
critique round for Debate. They count against those plans' usage limits
exactly as if you had typed them yourself.

## Security notes

- The dashboard listens on 127.0.0.1 only, so other machines cannot reach it.
- Every request from the page carries a random token created at startup, and
  the Host and Origin headers are checked, so other websites you have open
  cannot use it either.
- AI answers are rendered as Markdown through a sanitiser; scripts in an
  answer never run.
- Your logins stay in the tools' own stores (Claude) and in `.codex-home`
  (Codex). Neither is part of this repository.

## Changing the GPT model

`.codex-home/config.toml` sets the model and reasoning effort Codex uses.
If your plan does not offer the default, edit the `model` line.

## Files

| File | What it does |
|---|---|
| `conductor.py` | Runs the AIs, the critique rounds and the merge; saves the run folder |
| `dashboard.py` | Local web server for the page |
| `dashboard.html` | The page |
| `roles.json` | Role presets you can edit |
| `Start-Harness.cmd` | Double-click launcher |
| `Sign-In-Codex.cmd` | One-off Codex sign-in for this harness |
| `CLAUDE.md` | Tells Claude that harness runs are one-shot answers |

## Licence

MIT, see `LICENSE`.
