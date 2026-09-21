# Sidecall

Sidecall lets Claude Code ask the other AI command-line agents already on your computer (Codex, Antigravity, Grok,
Kimi) the same question at the same time, each through its own official app and your own subscription. You get
one report that shows, claim by claim, what different model families agree on, where they contradict each other,
and what only one of them said, plus a one-command second opinion and a critic from a different model family.

Not affiliated with Anthropic, OpenAI, Google, xAI or Moonshot AI.

**Status: v0.1.** Tested live on 21 September 2026 with Codex, Antigravity and Grok on their standard models; the
Kimi adapter follows Kimi Code CLI 0.41's own `--help`. 68 automated tests cover the rest with stand-in tools.

## Install

```
/plugin marketplace add vadimchernets/sidecall
/plugin install sidecall@sidecall
```

Then run `/sidecall:setup` once. It finds the tools you have and shows one fix for anything missing.

## The three commands you will use most

| command | what it does | example |
|---|---|---|
| `/sidecall:ask` | asks every working tool (one per model family, up to four) and writes a report | `/sidecall:ask "In what year did Parliament first sit in Canberra?"` |
| `/sidecall:second` | asks one tool from a different family and compares it with Claude's answer | `/sidecall:second "Is this migration safe to run twice?"` |
| `/sidecall:critic` | sends your diff, a file or a plan to a different family with orders to find what is wrong | `/sidecall:critic diff --focus "security"` |

You can also just say "ask the other AIs about this" or "get a second opinion on that". Sidecall runs only when
you ask for it in so many words: it never starts on its own.

## What it does NOT do

- It does not use API keys, and it does not buy anything. It uses the command-line tools you have already
  installed and signed in to.
- It does not sign you in, open login pages, or read your passwords or tokens.
- It does not decide who is right. It shows who said what, with quotes you can check.
- It does not guarantee the other tools cannot touch your files: only Codex runs in an operating-system sandbox
  (see below). It does not run on Windows in version 0.1.

## How it protects your files and quota

1. **Your own tools only.** Each tool is started on your machine through its official command-line app.
2. **Kept away from your files, as far as each tool allows.** Every tool starts in an empty scratch folder, never
   in your project. Only Codex runs in an operating-system read-only sandbox. The others are asked to stay
   read-only and are denied the tools Sidecall can deny (Grok's terminal; Kimi asks before any edit, and nobody is there to say yes), but
   they can still use their other built-in tools. The table under Configuration says what applies to each.
3. **No hidden spending.** Before anything is sent you see which tools, which model each will use, and the exact
   text. Sidecall sends no test prompts during a question. Every variable that looks like a key, token, secret
   or password is removed from the tools' environment, so they answer through your subscription, not a paid key.
4. **Secrets stay home.** If the text looks like it contains a key or token, Sidecall refuses to send it and
   names the line. There is no override.
5. **Answers are data.** Nothing another model writes is ever followed as an instruction; suspicious text is
   flagged in the report. A hard time limit stops every tool and everything it started, and so does stopping
   Sidecall itself.

---

## Save your quota: the modes

Sidecall tells each tool exactly which model to use. Unless you choose `inherit`, it never relies on a tool's
hidden default, which is often its most expensive model. The first time, and whenever the mode changes, you see
this line before anything is sent (later runs show just its first sentence):

> Standard mode: codex = gpt-5.6-luna (medium), agy = gemini-3.8-flash-medium, grok = grok-4.7 (medium),
> kimi = kimi-code/kimi-for-coding. You can strengthen any of them (e.g. `--budget stronger` or
> `--model codex=gpt-5.6-terra`); a stronger model costs more and uses your quota faster.

| mode | what it means |
|---|---|
| `standard` (default) | economical everyday models at medium effort |
| `stronger` | stronger models or more effort: costs more, uses your quota faster |
| `strongest` | the top models at high effort: uses your quota faster than any other mode |
| `inherit` | no model flag: each tool uses its own settings (shown as "vendor default (may be its most expensive model)") |

The critic uses one tier up by default (standard becomes stronger), because a weak critic wastes the round; the
mode line says so, and `--budget standard` keeps it at standard.

Change the mode for good in `/sidecall:setup`, or for one run: `--budget stronger`. Change one tool for one run:
`--model codex=gpt-5.6-terra`. Order of precedence: a flag on the run, then your `tools.json` pin, then the mode.
The report records the model each tool reported about itself where the tool prints it (Codex does), and
otherwise the model that was requested, marked "(requested)". The model names live in `scripts/registry.json` as plain data; tool makers rename
models often, so check them with each tool's own model list if a run fails with "unknown model".

Antigravity has a small daily allowance, so Sidecall never spends it on a test call: its first real question is
its test.

## Configuration

Your settings live in the plugin's data folder (or `~/.sidecall/` when run by hand) in `tools.json`:

```json
{
  "budget": "standard",
  "tools": {
    "codex": {"enabled": true, "model": "gpt-5.6-terra"},
    "kimi":  {"enabled": false}
  }
}
```

- `enabled`: include the tool in councils. Claude itself is off by default: it is already the one asking.
- `model` / `effort`: pin one tool's model or effort regardless of mode.
- There is no setting to pass credentials through. Every run prints which variables were hidden.

| tool | family | what keeps it away from your files | enforced by |
|---|---|---|---|
| codex | OpenAI | read-only sandbox | the operating system |
| agy | Google | in print mode it cannot ask for permission, so tools that need it are refused | the tool itself |
| grok | xAI | its terminal tool is removed; its other built-in tools remain | the tool itself |
| kimi | Moonshot | its default mode asks before any edit or command; a one-shot prompt has nobody to say yes | the tool itself |
| claude (off) | Anthropic | plan permission mode | the tool itself |
| gemini (legacy, off) | Google | personal-account use stopped on 18 June 2026; use agy |

Time limits: council 300 s, second opinion 180 s, critic 600 s (`--ceiling SECONDS`). At the limit, whatever a
tool has written is kept and marked `partial`. Only tools that print as they go leave a partial answer (Antigravity
does); Codex and Grok print once at the end, so a cut-off run of theirs shows `timeout` with nothing kept.

## What the report looks like

```
# Sidecall council
Question: In what year did Parliament first sit in Canberra?
Asked codex (gpt-5.6-luna, medium), grok (grok-4.7, medium), agy (gemini-3.8-flash-medium, medium): 3 answered.

## Bottom line
- Canberra has been the seat of Parliament since the 1920s. [codex, grok, agy]
- The exact year is disputed: 1927 against 1929. [codex, grok]

## Disputed
- c2 Parliament first sat in Canberra in 1927.
  - For it: codex, agy (about 1.5 independent voices)
  - Against it: grok, saying: 1929 (about 1.0 independent voices)

## How independent are these answers?
3 usable answers = about 1.8 independent voices under assumed correlations: same family 0.9, same cluster 0.6,
otherwise 0.35.
```

Every claim carries a quote from each tool's saved answer, and a script checks that each quote really is in
that answer (a `...` inside a quote may skip at most 80 characters). Sections: Bottom line, Who answered, Agreed
across families, Disputed, Echo (the same thing said inside one family counts as one voice), Single-source, Minority report, Could not be verified, How independent,
Agreement within one cluster (different families that are assumed to be partly alike, such as several Chinese
labs' models), Checks, Claude's own view (written last, never counted), Files. With fewer than two usable answers the report
says: "This is one opinion, not a comparison."

The independence numbers are assumptions, not measurements. To measure your own models, use the teaching
calculator in https://github.com/vadimchernets/after-chat.

## Troubleshooting

| you see | do this |
|---|---|
| `not signed in` | run the tool's login command in a terminal (for example `codex login`), then ask again |
| `out of quota or rate-limited` | nothing: the tool is paused for 6 hours so you do not wait on it; `/sidecall:setup --recheck` clears it |
| `not installed` | install the tool, or ignore it; the others still run |
| `partial` | the tool was cut off at the time limit; raise `--ceiling` or ask a shorter question |
| `timeout` (wrote nothing) | the tool prints only at the end and ran out of time; raise `--ceiling` or ask a shorter question |
| `suspect` (empty or refusal) | open its file in the run folder; Grok sometimes ends early if it tries a blocked tool |
| `REFUSED: ... looks like a secret` | remove the key (or replace it with a placeholder such as `<KEY>`) and ask again |
| `unknown model` in a tool's error | the maker renamed it: set `--model tool=<new id>` or edit `tools.json` |
| `WARNING: --help lacks ...` in setup | the tool changed its options; that tool may fail until Sidecall is updated |
| three tools fail within seconds | probably one shared cause: network, proxy or VPN |

## FAQ

**Does this cost money?** It uses the plans you already pay for. Each question uses a little of each tool's
quota; the mode line tells you which models before anything is sent.

**Why not ask ten models?** Models from one family share training data and tend to agree with each other. Four
answers from four families tell you more than ten from two.

**Why does Claude write its own answer first?** So the others cannot sway it. Its view appears last, labelled,
and is never counted as a vote.

**Can I run it without Claude Code?** Partly. `python3 scripts/fanout.py run --question-file q.txt --ceiling 120`
asks the tools and saves every answer in a run folder you can read. The claim-by-claim comparison needs Claude (or
you) to write `claims.json`; without it, `python3 scripts/merge.py render <run folder>` only lists who answered.

**Windows?** Not in version 0.1: stopping a tool together with everything it started needs macOS or Linux.

## How it relates to the paper

Sidecall is a working version of the fan-out, provenance and independence ideas in "After Chat: The Three
Transitions Between Non-Programmers and Agentic AI" (Vadym Chernets, 2026). The paper's teaching code and the
calculator for measuring correlation between models are at https://github.com/vadimchernets/after-chat.

To ask chat sites in your browser instead of command-line tools, see [Roundcall](https://github.com/vadimchernets/roundcall).

## Security

To report a vulnerability, open a private security advisory on GitHub (see `SECURITY.md`).

## Licence

Apache License 2.0, copyright 2026 Vadym Chernets. See `LICENSE` and `NOTICE`. The licence does not grant
rights to the name Sidecall (section 6); a fork should use its own name. The author develops multi-model orchestration methods and has filed related patent applications (pending; no
patent has been granted).
