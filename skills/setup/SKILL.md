---
name: setup
description: Find the AI command-line agents on this machine, show which are installed and signed in with one fix per problem, choose the mode (standard, stronger, strongest or inherit: which model and effort each tool uses), and reprint the last run with --status. Run /sidecall:setup first after installing Sidecall.
argument-hint: "[--status | --recheck | --budget standard|stronger|strongest|inherit]"
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fanout.py *)
---

# Sidecall setup

FANOUT = `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/fanout.py --state "${CLAUDE_PLUGIN_DATA}"`. User input: $ARGUMENTS

## `--status`
Run `FANOUT last` and show it as is: the last run line, each tool's status, the report path, and tool health
(which tools are paused and for how long). Stop.

## Normal setup (and `--recheck`)
1. `FANOUT discover --write` (free: it only checks versions and `--help`). Show the table in plain words:
   tool, found or not, on or off, the model it will use, and one fix-it line for anything missing
   ("install: ...", "then run: codex login"). Point out any "WARNING: --help lacks ..." line: that tool changed
   its flags and may fail until Sidecall is updated.
2. **Mode**, asked once. If the user passed `--budget`, use it. Otherwise show the mode line that `discover`
   printed ("Standard mode: codex = gpt-5.6-luna (medium), ...") and ask: "Keep standard (economical models at
   medium effort, the default), or switch to stronger (a stronger model costs more and uses your quota faster),
   strongest (top models: uses your quota faster than any other mode) or inherit (no model flag: each tool uses
   its own settings, whose default is often its most expensive model)?" If they do not answer, keep standard.
   Then `FANOUT budget <mode>` and show its mode line. Explain: outside inherit, Sidecall always names the model
   explicitly and never relies on a tool's hidden default. One run can differ: `--budget stronger` or
   `--model codex=gpt-5.6-terra`. The critic uses one tier up by default.
3. **Sign-in check** (optional, costs one tiny request per tool). Ask: "Check sign-in now? It sends one short
   'OK' prompt to each tool, except Antigravity, whose small daily allowance is saved for real questions."
   If yes: `FANOUT probe` (with `--recheck` when the user asked for a recheck); it prints the exact text before
   sending. Use a Bash timeout of 120000. Show each tool's result with its fix line. A tool that failed is paused
   (30 minutes, or 6 hours for quota) so later runs do not wait on it. Councils never send this check on their
   own.
4. Finish with what works now, in one sentence, and the first thing to try:
   `/sidecall:ask "your question"` or "get a second opinion on that".

Never open login pages, never ask for passwords, tokens or API keys, and never read credential files. Signing
in is done by the user, in a terminal, with the tool's own login command.

To turn a tool on or off or pin a model, the user edits `tools.json` in the state folder (FANOUT prints where):
`"enabled": false` turns a tool off, `"model": "id"` and `"effort": "low|medium|high"` pin its model. There is
no way to pass credentials through: every variable that looks like a key, token, secret or password is removed
from the tools' environment.
