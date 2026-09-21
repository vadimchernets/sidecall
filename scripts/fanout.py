#!/usr/bin/env python3
"""fanout.py: ask the other AI command-line agents on this machine, in parallel, under one time limit.

Each tool runs through its own official CLI and the user's own sign-in. Every tool gets an empty scratch
folder, a closed stdin (unless the prompt travels on it), its own process group, an explicit model (never
the CLI's hidden default unless the user picks the inherit mode) and an environment without credentials. At the time ceiling the whole
process group is stopped and whatever the tool wrote is kept as a partial answer.

Commands:
    fanout.py discover [--write]              which CLIs are installed (free: version check only)
    fanout.py probe [--tools a,b] [--recheck] one-word live check, cached (costs a tiny bit of quota)
    fanout.py run --question-file FILE [...]  the council / second opinion / critic run
    fanout.py last                            reprint the last run and tool health
    fanout.py budget [standard|stronger|strongest|inherit]   show or set the mode (model and effort per tool)
    fanout.py new [--mode M]                  make an empty run folder (the skill writes question.md into it)
    fanout.py put RUN NAME                    write stdin to NAME (claims.json, own.md, report.md) in RUN

State lives in --state DIR, else $SIDECALL_HOME, else $CLAUDE_PLUGIN_DATA, else ~/.sidecall.
POSIX only (macOS, Linux). Python 3.9+, standard library only. Apache-2.0 licence, Vadym Chernets 2026.
"""
import argparse
import datetime
import hashlib
import json
import os
import pathlib
import random
import re
import shutil
import signal
import subprocess
import sys
import time

HERE = pathlib.Path(__file__).resolve().parent
PROBE_PROMPT = "Reply with exactly one word: OK"
PROBE_SECONDS = 25
VERSION_SECONDS = 5
CEILINGS = {"ask": 300, "second": 180, "critic": 600}
OUTPUT_CAP = 2 * 1024 * 1024
SUSPECT_CHARS = 40
LIVE_TTL = 30 * 60
FAIL_TTL = 30 * 60
QUOTA_TTL = 6 * 3600
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)|\r")
# Credentials never reach a tool: *_KEY, *_TOKEN, *_SECRET, *_PAT, anything with PASSWORD, and the bare names.
ENV_STRIP = re.compile(r"(?:_KEY|APIKEY|_TOKEN|_SECRET|_PAT|_KEY_ID)$|^(?:TOKEN|SECRET|APIKEY)$|PASSWORD|PASSWD", re.I)
SECRETS = [
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{22,})")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
]
ASK_SUFFIX = ("Answer from your own knowledge. Do not run commands, open files or browse unless the question "
              "cannot be answered otherwise. Be concise; state key numbers and dates explicitly; say when you "
              "are unsure.")
PUT_NAMES = {"claims.json", "own.md", "report.md", "notes.md"}
_LIVE = []  # process groups started by this run, stopped if fanout itself is interrupted


def _on_signal(signum, frame):
    """If fanout is stopped (Ctrl-C, a caller's timeout), stop every tool it started: no orphans."""
    for proc in list(_LIVE):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            if sig == signal.SIGTERM:
                try:
                    proc.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    pass
    raise SystemExit(128 + signum)


def spawn(argv, **kw):
    """Every child process goes through here: its own process group, registered for cleanup."""
    proc = subprocess.Popen(argv, start_new_session=True, **kw)
    _LIVE.append(proc)
    return proc


def stop_all():
    """Stop every process group this fanout started that may still be alive (normal exit, error or signal)."""
    for proc in list(_LIVE):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass
    del _LIVE[:]


def quick_run(argv, timeout, env):
    """Short helper calls (--version, --help): same cleanup rules as the real calls."""
    proc = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, env=env)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(proc)
        raise
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass
    return out.decode(errors="replace") + err.decode(errors="replace")


def guard_signals():
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not in the main thread (tests): nothing to guard
            pass


# ---------------------------------------------------------------- state and config

def state_dir(arg=None):
    for cand in (arg, os.environ.get("SIDECALL_HOME"), os.environ.get("CLAUDE_PLUGIN_DATA")):
        if cand and cand.strip() and "${" not in cand:
            return pathlib.Path(cand).expanduser().resolve()
    return pathlib.Path.home() / ".sidecall"


def read_json(path, default):
    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def load_registry(path=None):
    return read_json(path or os.environ.get("SIDECALL_REGISTRY") or HERE / "registry.json", None)


def load_user(state):
    return read_json(state / "tools.json", {"budget": None, "tools": {}})


TIER_UP = {"standard": "stronger", "stronger": "strongest", "strongest": "strongest", "inherit": "inherit"}
VENDOR_DEFAULT = "vendor default (may be its most expensive model)"


def budget_of(user, reg, override=None):
    b = override or user.get("budget") or reg.get("default_budget", "standard")
    if b not in reg["budgets"]:
        raise SystemExit(f"Unknown budget '{b}'. Use one of: {', '.join(reg['budgets'])}.")
    return b


def choice_for(name, spec, user, budget, overrides):
    """(model, effort, where from). Model: one-run override > tools.json > registry budget table. Always explicit."""
    row = (spec.get("models") or {}).get(budget) or {}
    if isinstance(row, str):
        row = {"model": row}
    u = user.get("tools", {}).get(name) or {}
    effort = u.get("effort") or row.get("effort")
    if not spec.get("effort_args"):
        effort = None
    if budget == "inherit":
        effort = u.get("effort") if spec.get("effort_args") else None
    if name in overrides:
        return overrides[name], effort, "this run"
    if u.get("model"):
        return u["model"], effort, "your tools.json"
    if row.get("model"):
        return row["model"], effort, f"{budget} mode"
    return None, effort, "your own CLI settings" if budget == "inherit" else ""


def model_for(name, spec, user, budget, overrides):
    m, _, src = choice_for(name, spec, user, budget, overrides)
    return m, src


def mode_notice(reg, user, budget, names, overrides, short=False):
    """The line printed before any spend, in setup, and in the report's run line (short after the first time)."""
    parts = []
    for n in names:
        spec = reg["tools"][n]
        m, e, _ = choice_for(n, spec, user, budget, overrides)
        parts.append(f"{n} = {m or VENDOR_DEFAULT}" + (f" ({e})" if e else ""))
    label = "Inherit mode (using your own CLI settings)" if budget == "inherit" else budget.capitalize() + " mode"
    head = label + ": " + ", ".join(parts) + "."
    if short:
        return head
    if budget == "strongest":
        return head + " Strongest mode uses your quota faster than any other mode."
    if budget == "inherit":
        return head + (" Sidecall passes no model flag, so each tool uses whatever its own settings pick; "
                       "a tool's default is often its most expensive model and uses your quota faster.")
    up = "strongest" if budget == "stronger" else "stronger"
    return head + (f" You can strengthen any of them (e.g. `--budget {up}` or `--model codex=gpt-5.6-terra`); "
                   "a stronger model costs more and uses your quota faster.")


def notice_for_run(reg, user, state, budget, names, overrides):
    """Full notice the first time and whenever the mode or models change; one short line otherwise."""
    full = mode_notice(reg, user, budget, names, overrides)
    seen = read_json(state / "notice.json", {})
    if seen.get("full") == full:
        return mode_notice(reg, user, budget, names, overrides, short=True)
    write_json(state / "notice.json", {"full": full, "at": int(time.time())})
    return full


def enabled(name, spec, user):
    u = user.get("tools", {}).get(name) or {}
    return u.get("enabled", spec.get("default_on", False))


# ---------------------------------------------------------------- breaker (probe cache)

def breaker_get(state, name):
    b = read_json(state / "breaker.json", {})
    e = b.get(name)
    if e and e.get("until", 0) > time.time():
        return e
    return None


def breaker_set(state, name, status, why):
    b = read_json(state / "breaker.json", {})
    ttl = LIVE_TTL if status == "live" else QUOTA_TTL if status == "quota" else FAIL_TTL
    b[name] = {"status": status, "why": why[:200], "at": int(time.time()), "until": int(time.time() + ttl)}
    write_json(state / "breaker.json", b)


def breaker_clear(state):
    p = state / "breaker.json"
    if p.exists():
        p.unlink()


# ---------------------------------------------------------------- building a call

def child_env(user=None):
    """The tools' environment: the caller's, minus every credential-looking variable. No override in v0.1."""
    env = {k: v for k, v in os.environ.items() if not ENV_STRIP.search(k)}
    env["NO_COLOR"] = "1"
    env.setdefault("TERM", "dumb")
    return env, sorted(k for k in os.environ if ENV_STRIP.search(k))


def build_argv(spec, model, prompt, prompt_file, final_file, effort=None):
    out = []
    for part in spec["argv"]:
        if part == "{model_args}":
            if model and spec.get("model_flag"):
                out += [spec["model_flag"], model]
        elif part == "{effort_args}":
            if effort and spec.get("effort_args"):
                out += [a.replace("{effort}", effort) for a in spec["effort_args"]]
        elif part == "{prompt}":
            out.append(prompt)
        elif part == "{prompt_file}":
            out.append(str(prompt_file))
        elif part == "{final_file}":
            out.append(str(final_file))
        else:
            out.append(part)
    return out


def shown_argv(argv, prompt):
    return " ".join(f"<prompt: {len(prompt.encode())} bytes>" if a == prompt and len(a) > 60 else a for a in argv)


def scan_secrets(text):
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for label, pat in SECRETS:
            if pat.search(line):
                hits.append(f"line {n}: looks like a {label}")
    return hits


def classify_text(text, spec, reg):
    low = text.lower()
    for kind, key in (("quota", "quota_patterns"), ("not_logged_in", "auth_patterns")):
        for p in spec.get(key) or []:
            if re.search(p, low):
                return kind
    return ""


def stop_group(proc):
    """SIGTERM to the whole process group, 3 s grace, then SIGKILL. No orphans."""
    for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 5)):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            break
        except OSError:
            try:
                proc.send_signal(sig)
            except OSError:
                break
        try:
            proc.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    try:  # children that outlived the leader still carry the group id
        os.killpg(proc.pid, signal.SIGKILL)
    except OSError:
        pass


# ---------------------------------------------------------------- discover and probe

def discover(reg, user):
    env, _ = child_env(user)
    rows = []
    for name, spec in reg["tools"].items():
        path = shutil.which(spec["binary"])
        row = {"tool": name, "family": spec["family"], "found": bool(path), "path": path or "",
               "version": "", "enabled": enabled(name, spec, user), "missing_flags": []}
        if path:
            try:
                v = quick_run(spec["version_cmd"], VERSION_SECONDS, env).strip()
                row["version"] = v.splitlines()[0][:80] if v else ""
            except (OSError, subprocess.TimeoutExpired):
                row["version"] = "no answer to --version"
            if spec.get("help_cmd") and spec.get("expected_flags"):
                try:
                    text = quick_run(spec["help_cmd"], VERSION_SECONDS, env)
                    row["missing_flags"] = [f for f in spec["expected_flags"] if f not in text]
                except (OSError, subprocess.TimeoutExpired):
                    pass
        rows.append(row)
    return rows


def run_one_probe(name, spec, choice, state, env):
    model, effort = choice
    work = state / "probe" / name
    work.mkdir(parents=True, exist_ok=True)
    pf = work / "prompt.md"
    pf.write_text(PROBE_PROMPT, encoding="utf-8")
    ff = work / "final.md"
    if ff.exists():
        ff.unlink()
    argv = build_argv(spec, model, PROBE_PROMPT, pf, ff, effort)
    stdin = open(pf, "rb") if spec["prompt_via"] == "stdin" else subprocess.DEVNULL
    try:
        proc = spawn(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=stdin, cwd=work, env=env)
    except FileNotFoundError:
        return "not_installed", "not installed"
    finally:
        if stdin is not subprocess.DEVNULL:
            stdin.close()
    try:
        out, err = proc.communicate(timeout=PROBE_SECONDS)
    except subprocess.TimeoutExpired:
        stop_group(proc)
        return "error", f"no answer within {PROBE_SECONDS} s"
    try:
        os.killpg(proc.pid, signal.SIGKILL)  # nothing the probe started outlives it
    except OSError:
        pass
    text = out.decode(errors="replace") + "\n" + err.decode(errors="replace")
    answer = out.decode(errors="replace").strip() or (ff.read_text(errors="replace").strip() if ff.exists() else "")
    if proc.returncode == 0 and answer:
        return "live", "answered the probe"
    if proc.returncode == 0:
        return "error", "exit 0 but no answer"
    kind = classify_text(err.decode(errors="replace") + "\n" + out.decode(errors="replace")[:400], spec, None)
    return (kind or "error"), f"exit {proc.returncode}: {ANSI.sub('', text).strip()[:160]}"


def probe(reg, user, state, names, budget, overrides, recheck=False, force_scarce=False):
    env, _ = child_env(user)
    results = {}
    procs = []
    for name in names:
        spec = reg["tools"][name]
        cached = None if recheck else breaker_get(state, name)
        if cached:
            results[name] = {"status": cached["status"], "why": cached["why"] + " (cached)"}
            continue
        if spec.get("scarce_quota") and not force_scarce:
            results[name] = {"status": "unprobed", "why": "small daily allowance: not probed, first real call decides"}
            continue
        if not shutil.which(spec["binary"]):
            results[name] = {"status": "not_installed", "why": f"not installed ({spec.get('install_hint', '')})"}
            continue
        model, effort, _ = choice_for(name, spec, user, budget, overrides)
        procs.append((name, spec, (model, effort)))
    # probes run one after another in threads-free fashion but each is short; keep it simple and bounded
    import concurrent.futures as cf
    with cf.ThreadPoolExecutor(max_workers=max(1, len(procs))) as ex:
        futs = {ex.submit(run_one_probe, n, s, m, state, env): n for n, s, m in procs}
        for f in cf.as_completed(futs):
            n = futs[f]
            status, why = f.result()
            results[n] = {"status": status, "why": why}
            if status != "not_installed":
                breaker_set(state, n, status, why)
    return results


# ---------------------------------------------------------------- the run

def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40].strip("-")
    return s or "run"


def compose_prompt(mode, question, subject, focus, raw):
    if mode == "critic":
        brief = (HERE / "briefs" / "critic.md").read_text(encoding="utf-8")
        body = brief.replace("{focus}", focus or "correctness, security, and anything that would surprise a user")
        return body.replace("{subject}", subject or question)
    if raw:
        return question
    return f"{question.rstrip()}\n\n---\n{ASK_SUFFIX}\n"


def pick_tools(reg, user, state, args, budget, overrides):
    """Return (chosen, skipped{name: reason}) before probing."""
    skipped = {}
    if args.tools:
        wanted = [t.strip() for t in args.tools.split(",") if t.strip()]
        unknown = [t for t in wanted if t not in reg["tools"]]
        if unknown:
            raise SystemExit(f"Unknown tool(s): {', '.join(unknown)}. Known: {', '.join(reg['tools'])}.")
    else:
        wanted = [t for t in reg["preference"] if t in reg["tools"] and enabled(t, reg["tools"][t], user)]
    exclude = {f.strip() for f in (args.exclude_family or "").split(",") if f.strip()}
    chosen = []
    for name in wanted:
        spec = reg["tools"][name]
        if not SAFE_NAME.match(name):
            skipped[name] = "unsafe tool name"
            continue
        if spec["family"] in exclude:
            skipped[name] = f"family {spec['family']} excluded for this run"
            continue
        if spec.get("legacy") and not args.tools:
            skipped[name] = "legacy: " + spec["legacy"]
            continue
        if not shutil.which(spec["binary"]):
            skipped[name] = f"not installed -> {spec.get('install_hint', 'install it first')}"
            continue
        b = breaker_get(state, name)
        if b and b["status"] != "live":
            hint = spec.get("login_hint") if b["status"] == "not_logged_in" else "try again later"
            skipped[name] = f"{b['status'].replace('_', ' ')} (seen {int((time.time() - b['at']) / 60)} min ago) -> {hint}"
            continue
        model, _ = model_for(name, spec, user, budget, overrides)
        if spec.get("model_required") and not model and budget != "inherit":
            skipped[name] = "no model set for this budget (Sidecall never uses a hidden default) -> set one in tools.json"
            continue
        chosen.append(name)
    return chosen, skipped


def limit_panel(reg, chosen, n):
    """One tool per family, in preference order, at most n."""
    out, fams = [], set()
    for name in chosen:
        f = reg["tools"][name]["family"]
        if f in fams:
            continue
        out.append(name)
        fams.add(f)
        if len(out) >= n:
            break
    return out


def run_tools(reg, user, names, prompt, run, ceiling, budget, overrides):
    env, stripped = child_env(user)
    (run / "answers").mkdir(parents=True, exist_ok=True)
    (run / "stderr").mkdir(exist_ok=True)
    (run / "raw").mkdir(exist_ok=True)
    pf = run / "prompt.md"
    running, results = {}, {}
    for name in names:
        spec = reg["tools"][name]
        model, effort, _ = choice_for(name, spec, user, budget, overrides)
        work = run / "work" / name
        work.mkdir(parents=True, exist_ok=True)
        final = run / "raw" / f"{name}.final.md"
        if spec["prompt_via"] == "arg" and len(prompt.encode()) > spec.get("max_arg_bytes", 120000):
            results[name] = {"status": "failed", "note": f"prompt too large for {name} "
                             f"({len(prompt.encode())} bytes > {spec.get('max_arg_bytes')}); shorten it",
                             "model": model, "elapsed_s": 0.0}
            continue
        argv = build_argv(spec, model, prompt, pf, final, effort)
        out = open(run / "raw" / f"{name}.out", "wb")
        err = open(run / "stderr" / f"{name}.txt", "wb")
        stdin = open(pf, "rb") if spec["prompt_via"] == "stdin" else subprocess.DEVNULL
        try:
            proc = spawn(argv, stdout=out, stderr=err, stdin=stdin, cwd=work, env=env)
        except OSError as e:
            out.close()
            err.close()
            results[name] = {"status": "not_installed" if isinstance(e, FileNotFoundError) else "failed",
                             "note": str(e)[:120], "model": model, "elapsed_s": 0.0}
            continue
        finally:
            if stdin is not subprocess.DEVNULL:
                stdin.close()
        running[name] = {"proc": proc, "out": out, "err": err, "t0": time.monotonic(), "model": model,
                         "effort": effort,
                         "final": final, "argv": shown_argv(argv, prompt)}
    deadline = time.monotonic() + ceiling
    while running:
        now = time.monotonic()
        for name in list(running):
            r = running[name]
            raw_size = (run / "raw" / f"{name}.out").stat().st_size
            capped = raw_size > OUTPUT_CAP
            if r["proc"].poll() is None and not capped and now < deadline:
                continue
            flags = []
            if r["proc"].poll() is None:
                stop_group(r["proc"])
                flags.append("capped" if capped else "ceiling")
            else:
                try:  # the tool is done; anything it left running in its group goes too
                    os.killpg(r["proc"].pid, signal.SIGKILL)
                except OSError:
                    pass
            r["out"].close()
            r["err"].close()
            results[name] = {"exit": r["proc"].returncode, "elapsed_s": round(time.monotonic() - r["t0"], 1),
                             "model": r["model"], "effort": r["effort"], "flags": flags, "argv": r["argv"],
                             "final": str(r["final"])}
            del running[name]
        if running:
            time.sleep(0.2)
    return results, stripped


def finish(reg, name, res, run):
    """Turn raw output into answers/<name>.md and a status."""
    spec = reg["tools"][name]
    if "status" in res:
        return res
    raw = (run / "raw" / f"{name}.out").read_bytes()[:OUTPUT_CAP].decode("utf-8", errors="replace")
    final = pathlib.Path(res.pop("final"))
    text = final.read_text(encoding="utf-8", errors="replace") if final.exists() and final.stat().st_size else raw
    text = ANSI.sub("", text).strip()
    err = ANSI.sub("", (run / "stderr" / f"{name}.txt").read_text(encoding="utf-8", errors="replace"))
    flags = res.get("flags", [])
    if flags:
        status = "partial" if text else "timeout"
    elif res["exit"] != 0:
        # only a failed call is classified: stderr, plus stdout when it is short enough to be an error line
        status = classify_text(err[-4000:] + "\n" + (text if len(text) < 400 else ""), spec, reg) or "failed"
    elif len(text) < SUSPECT_CHARS or any(p in text.lower()[:300] for p in reg.get("suspect_patterns", [])):
        status = "suspect"
        res["note"] = "empty or very short answer" if len(text) < SUSPECT_CHARS else "looks like a refusal"
    else:
        status = "ok"
    if status == "suspect" and classify_text(err[-4000:], spec, reg):
        res["note"] += "; its error output mentions a limit or sign-in problem"
    if status in ("failed", "not_logged_in", "quota") and not res.get("note"):
        last = [ln for ln in err.strip().splitlines() if ln.strip()][-1:] or [""]
        res["note"] = last[0][:160]
    if text:
        (run / "answers" / f"{name}.md").write_text(text + "\n", encoding="utf-8")
    reported = {}
    for key, pat in (spec.get("report_patterns") or {}).items():
        m = re.search(pat, err + "\n" + raw[:3000], re.M | re.I)
        if m:
            reported[key] = m.group(1).strip()
    asked_model, asked_effort = res.get("model"), res.get("effort")
    if reported.get("model"):
        res["model"] = reported["model"] + (f", {reported.get('effort') or asked_effort}"
                                            if (reported.get("effort") or asked_effort) else "") + " (reported by the tool)"
    elif asked_model:
        res["model"] = asked_model + (f", {asked_effort}" if asked_effort else "") + " (requested)"
    else:
        res["model"] = "vendor default (may be its most expensive model)"
    res["model_requested"] = asked_model
    res.update({"status": status, "chars": len(text), "family": spec["family"]})
    return res


def fix_line(reg, name, res):
    spec = reg["tools"][name]
    s = res["status"]
    if s == "not_logged_in":
        return f"{name}: not signed in -> run `{spec.get('login_hint')}` in a terminal, then try again"
    if s == "quota":
        return f"{name}: out of quota or rate-limited -> skipped for 6 hours; try again later"
    if s == "timeout":
        return f"{name}: wrote nothing before the time limit -> raise --ceiling or ask a shorter question"
    if s == "suspect":
        return f"{name}: {res.get('note', 'suspicious answer')} -> left out of the comparison; see its file"
    if s == "failed":
        return f"{name}: failed ({res.get('note', '')}) -> run `{spec['binary']} --help` or `/sidecall:setup`"
    return ""


def cmd_run(args, reg):
    state = state_dir(args.state)
    user = load_user(state)
    overrides = {}
    for m in args.model or []:
        if "=" not in m:
            raise SystemExit("--model takes tool=model, e.g. --model codex=gpt-5.6-terra")
        k, v = m.split("=", 1)
        overrides[k.strip()] = v.strip()
    budget = budget_of(user, reg, args.budget)
    tier_note = ""
    if args.mode == "critic" and not args.budget and TIER_UP.get(budget, budget) != budget:
        tier_note = f"Critic uses one tier up by default: {budget} -> {TIER_UP[budget]}. "
        budget = TIER_UP[budget]
    if args.question_file == "-":
        question = sys.stdin.read()
    elif args.question_file:
        question = pathlib.Path(args.question_file).read_text(encoding="utf-8")
    else:
        question = args.question or ""
    subject = ""
    if args.subject_file == "-":
        subject = sys.stdin.read()
    elif args.subject_file:
        subject = pathlib.Path(args.subject_file).read_text(encoding="utf-8", errors="replace")
    elif args.subject_diff:
        try:
            d = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            subject = d.stdout if d.returncode == 0 and d.stdout.strip() else subprocess.run(
                ["git", "diff"], capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout
        except OSError:
            raise SystemExit("Git is not installed (or not on PATH), so there is no diff to read. "
                             "Name a file instead: --subject-file PATH.")
        if not subject.strip():
            raise SystemExit("No diff found in this folder (git diff is empty). Name a file instead: --subject-file PATH.")
    if not question.strip() and not subject.strip():
        if args.mode == "critic":
            raise SystemExit("Nothing to review. Give --subject-file PATH, --subject-file - (text on stdin) "
                             "or --subject-diff (the current git diff).")
        raise SystemExit("No question given. Use --question-file FILE (or - for stdin).")
    prompt = compose_prompt(args.mode, question, subject, args.focus, args.raw)
    secrets = scan_secrets(prompt)
    if secrets:
        print("REFUSED: the text to be sent looks like it contains a secret, so nothing was sent:")
        for s in secrets:
            print("  - " + s)
        print("Remove it (or replace it with a placeholder such as <KEY>) and ask again.")
        return 3
    chosen, skipped = pick_tools(reg, user, state, args, budget, overrides)
    ceiling = args.ceiling or CEILINGS[args.mode]
    if args.dry_run:
        panel = limit_panel(reg, chosen, args.pick or reg.get("max_panel", 4)) if not args.tools else chosen
        print(f"DRY RUN (nothing sent, no quota spent). Mode {args.mode}, ceiling {ceiling:.0f} s.")
        print(tier_note + mode_notice(reg, user, budget, panel, overrides))
        for name in panel:
            spec = reg["tools"][name]
            model, effort, src = choice_for(name, spec, user, budget, overrides)
            argv = build_argv(spec, model, prompt, "<run>/prompt.md", "<run>/raw/final.md", effort)
            print(f"  would ask {name} ({spec['family']}), model {model or VENDOR_DEFAULT}"
                  f"{' at ' + effort + ' effort' if effort else ''} [{src}]\n"
                  f"    {shown_argv(argv, prompt)}  prompt via {spec['prompt_via']}")
        for name, why in skipped.items():
            print(f"  skip {name}: {why}")
        print(f"Text to be sent ({len(prompt.encode())} bytes):\n-----\n{prompt}\n-----")
        return 0
    live_note = {}
    pre_panel = chosen if args.tools else limit_panel(reg, chosen, args.pick or reg.get("max_panel", 4))
    notice = tier_note + notice_for_run(reg, user, state, budget, pre_panel, overrides)
    print(notice, flush=True)  # before anything is sent, probes included
    if args.probe and chosen:
        print(f'Checking sign-in first (--probe): sending "{PROBE_PROMPT}" to '
              f'{", ".join(n for n in chosen if not reg["tools"][n].get("scarce_quota"))}.', flush=True)
        pr = probe(reg, user, state, chosen, budget, overrides)
        for name, r in pr.items():
            if r["status"] not in ("live", "unprobed"):
                skipped[name] = f"{r['status'].replace('_', ' ')}: {r['why']}"
            else:
                live_note[name] = r["status"]
        chosen = [n for n in chosen if n not in skipped]
    panel = chosen if args.tools else limit_panel(reg, chosen, args.pick or reg.get("max_panel", 4))
    need = 1 if args.mode != "ask" else 2
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run = (pathlib.Path(args.out) if args.out else state / "runs" / f"{ts}-{args.mode}-{slug(question or 'critic')}")
    run = run.expanduser().resolve()  # tools run in their own folders: every path handed to them must be absolute
    run.mkdir(parents=True, exist_ok=True)
    (run / "prompt.md").write_text(prompt, encoding="utf-8")
    if not panel:
        meta = {"question": question.strip(), "title": f"Sidecall {args.mode}", "sources": {},
                "run_line": "No tool could be asked. " + "; ".join(f"{k}: {v}" for k, v in skipped.items())}
        write_json(run / "meta.json", meta)
        write_json(state / "last.json", {"run": str(run)})
        print(meta["run_line"])
        print("Nothing was sent. Run /sidecall:setup to see what to fix.")
        print(f"RUN_DIR={run}")
        return 4
    results, stripped = run_tools(reg, user, panel, prompt, run, ceiling, budget, overrides)
    sources = {}
    for name in panel:
        res = finish(reg, name, results[name], run)
        if res["status"] in ("quota", "not_logged_in"):
            breaker_set(state, name, res["status"], res.get("note", ""))
        elif res["status"] in ("ok", "partial"):
            breaker_set(state, name, "live", "answered a real question")
        sources[name] = {k: v for k, v in res.items() if k not in ("exit", "argv")}
    for name, why in skipped.items():
        sources.setdefault(name, {"status": "skipped", "note": why, "family": reg["tools"][name]["family"]}
                           if name in reg["tools"] else {"status": "skipped", "note": why})
    usable = [n for n, s in sources.items() if s.get("status") in ("ok", "partial")]
    fast_fail = [n for n, s in sources.items() if s.get("status") in ("failed", "not_logged_in", "quota")
                 and (s.get("elapsed_s") or 99) < 5]
    counts = bump_calls(state, args.session, panel)
    asked = ", ".join(f"{n} ({sources[n].get('model') or VENDOR_DEFAULT})" for n in panel)
    line = f"Asked {asked}: {len(usable)} answered."
    extra = [f"{n} {s['status'].replace('_', ' ')}" for n, s in sources.items() if n in panel and s["status"] not in ("ok",)]
    if extra:
        line += " " + "; ".join(extra) + "."
    if skipped:
        line += " Skipped: " + "; ".join(f"{k} ({v})" for k, v in skipped.items()) + "."
    line += f" Budget {budget}. Ceiling {ceiling:.0f} s. Calls this session: " + \
        ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + "."
    meta = {"question": question.strip() if question.strip() else "(critique of the attached subject)",
            "title": {"ask": "Sidecall council", "second": "Sidecall second opinion",
                      "critic": "Sidecall critique"}[args.mode],
            "run_line": line + " " + notice, "mode": args.mode, "budget": budget, "ceiling_s": ceiling,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "sources": sources,
            "env_stripped": stripped, "created": ts}
    write_json(run / "meta.json", meta)
    if args.mode == "ask" and args.blind and len(usable) >= 2:
        make_blind(run, usable)
    write_json(state / "last.json", {"run": str(run)})
    print(line)
    for name in panel:
        fl = fix_line(reg, name, sources[name])
        if fl:
            print("  " + fl)
    if len(fast_fail) >= 3:
        print(f"  {len(fast_fail)} tools failed within 5 s: probably one shared cause (network, proxy or VPN), "
              f"not {len(fast_fail)} separate problems.")
    if stripped:
        print(f"  Hidden from the tools: {len(stripped)} API-key/token variable(s), so each answers through its "
              f"own sign-in (names: {', '.join(stripped)}).")
    if len(usable) < need:
        print("  Fewer than two usable answers: this is one opinion, not a comparison." if args.mode == "ask"
              else "  No usable answer came back.")
    print(f"RUN_DIR={run}")
    return 0 if len(usable) >= need else 5


def make_blind(run, usable):
    labels = [chr(ord("A") + i) for i in range(len(usable))]
    order = list(usable)
    random.shuffle(order)
    (run / "blind").mkdir(exist_ok=True)
    mapping = {}
    for label, name in zip(labels, order):
        shutil.copyfile(run / "answers" / f"{name}.md", run / "blind" / f"{label}.md")
        mapping[label] = name
    write_json(run / "blind" / "map.json", mapping)


def bump_calls(state, session, names):
    p = state / "calls.json"
    data = read_json(p, {})
    key = session if session and "${" not in session else datetime.date.today().isoformat()
    if data.get("key") != key:
        data = {"key": key, "counts": {}}
    for n in names:
        data["counts"][n] = data["counts"].get(n, 0) + 1
    write_json(p, data)
    return data["counts"]


# ---------------------------------------------------------------- other commands

def cmd_discover(args, reg):
    state = state_dir(args.state)
    user = load_user(state)
    rows = discover(reg, user)
    budget = budget_of(user, reg)
    if args.write:
        user.setdefault("tools", {})
        for r in rows:
            u = user["tools"].setdefault(r["tool"], {})
            u["found"] = r["path"]
            u["version"] = r["version"]
            u.setdefault("enabled", reg["tools"][r["tool"]].get("default_on", False) and r["found"])
        user.setdefault("budget", budget)
        if not user.get("budget"):
            user["budget"] = budget
        write_json(state / "tools.json", user)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    found = [r["tool"] for r in rows if r["found"] and r["enabled"]]
    print(mode_notice(reg, user, budget, found, {}) if found else f"{budget.capitalize()} mode.")
    print("Change the mode: fanout.py budget standard|stronger|strongest|inherit")
    print(f"{'tool':8} {'family':10} {'found':6} {'on':4} {'model':34} note")
    for r in rows:
        spec = reg["tools"][r["tool"]]
        model, effort, _ = choice_for(r["tool"], spec, user, budget, {})
        model = (model or "-") + (f" ({effort})" if effort and model else "")
        b = breaker_get(state, r["tool"])
        if not r["found"]:
            note = f"install: {spec.get('install_hint')}"
        elif spec.get("legacy"):
            note = "legacy: " + spec["legacy"]
        elif b and b["status"] != "live":
            note = f"{b['status']} -> {spec.get('login_hint') if b['status'] == 'not_logged_in' else 'try later'}"
        elif b:
            note = "signed in (checked recently)"
        elif spec.get("scarce_quota"):
            note = "not probed (small daily allowance); first real call decides"
        else:
            note = f"sign-in not checked yet; if it fails run `{spec.get('login_hint')}`"
        if r["missing_flags"]:
            note += f"; WARNING: --help lacks {', '.join(r['missing_flags'])}"
        if spec.get("experimental"):
            note += "; experimental"
        print(f"{r['tool']:8} {r['family']:10} {'yes' if r['found'] else 'no':6} "
              f"{'yes' if r['enabled'] else 'no':4} {model[:34]:34} {note}")
    return 0


def cmd_probe(args, reg):
    state = state_dir(args.state)
    if args.recheck:
        breaker_clear(state)
    user = load_user(state)
    budget = budget_of(user, reg, args.budget)
    names = [t for t in (args.tools.split(",") if args.tools else reg["tools"]) if t in reg["tools"]]
    names = [n for n in names if shutil.which(reg["tools"][n]["binary"]) and not reg["tools"][n].get("legacy")]
    sent = [n for n in names if args.include_scarce or not reg["tools"][n].get("scarce_quota")]
    print(f'Sending "{PROBE_PROMPT}" to {", ".join(sent) or "no tool"} '
          f'(tools checked in the last 30 minutes are not asked again).', flush=True)
    res = probe(reg, user, state, names, budget, {}, recheck=args.recheck, force_scarce=args.include_scarce)
    if args.json:
        print(json.dumps(res, indent=2))
    else:
        for n, r in res.items():
            hint = reg["tools"][n].get("login_hint") if r["status"] == "not_logged_in" else ""
            print(f"{n:8} {r['status']:14} {r['why']}" + (f" -> run `{hint}`" if hint else ""))
    return 0


def cmd_last(args, reg):
    state = state_dir(args.state)
    last = read_json(state / "last.json", {})
    run = pathlib.Path(last.get("run", ""))
    if not last or not (run / "meta.json").exists():
        print("No run yet. Try /sidecall:ask \"your question\".")
    else:
        meta = read_json(run / "meta.json", {})
        print(f"Last run: {run}")
        print(meta.get("run_line", ""))
        for n, s in sorted(meta.get("sources", {}).items()):
            print(f"  {n:8} {s.get('status', ''):14} {s.get('model') or ''} {s.get('note') or ''}")
        rep = run / "report.md"
        print(f"Report: {rep}" if rep.exists() else "Report: not written yet")
    b = read_json(state / "breaker.json", {})
    if b:
        print("Tool health (cached):")
        for n, e in sorted(b.items()):
            left = int((e.get("until", 0) - time.time()) / 60)
            if left > 0:
                print(f"  {n:8} {e['status']:14} for {left} more min: {e.get('why', '')[:80]}")
    return 0


def cmd_budget(args, reg):
    state = state_dir(args.state)
    user = load_user(state)
    if args.mode:
        if args.mode not in reg["budgets"]:
            raise SystemExit(f"Use one of: {', '.join(reg['budgets'])}.")
        user["budget"] = args.mode
        write_json(state / "tools.json", user)
    b = budget_of(user, reg)
    print(f"Mode: {b} ({reg['budgets'][b]})")
    names = [n for n, spec in reg["tools"].items() if not spec.get("legacy")]
    print(mode_notice(reg, user, b, names, {}))
    return 0


def cmd_new(args, reg):
    """Make an empty run folder so the question can be written there as a file (never through a shell)."""
    state = state_dir(args.state)
    ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    run = state / "runs" / f"{ts}-{args.mode}"
    n = 1
    while run.exists():
        n += 1
        run = state / "runs" / f"{ts}-{args.mode}-{n}"
    run.mkdir(parents=True)
    print(f"Write the question to {run / 'question.md'} (critic: the text to review to {run / 'subject.md'}).")
    print(f"RUN_DIR={run}")
    return 0


def cmd_put(args, reg):
    run = pathlib.Path(args.run).resolve()
    if args.name not in PUT_NAMES:
        raise SystemExit(f"put writes only {', '.join(sorted(PUT_NAMES))}")
    if not (run / "prompt.md").exists():
        raise SystemExit(f"{run} is not a Sidecall run folder (no prompt.md)")
    data = sys.stdin.read()
    if args.name.endswith(".json"):
        json.loads(data)
    (run / args.name).write_text(data, encoding="utf-8")
    print(f"wrote {run / args.name} ({len(data.encode())} bytes)")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--state", help="state folder (default: $SIDECALL_HOME, $CLAUDE_PLUGIN_DATA or ~/.sidecall)")
    ap.add_argument("--registry", help="alternative registry.json (tests)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("discover")
    p.add_argument("--write", action="store_true")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("probe")
    p.add_argument("--tools")
    p.add_argument("--recheck", action="store_true")
    p.add_argument("--include-scarce", action="store_true")
    p.add_argument("--budget")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--mode", choices=list(CEILINGS), default="ask")
    p.add_argument("--question-file")
    p.add_argument("--question")
    p.add_argument("--subject-file")
    p.add_argument("--subject-diff", action="store_true")
    p.add_argument("--focus")
    p.add_argument("--tools")
    p.add_argument("--exclude-family")
    p.add_argument("--pick", type=int)
    p.add_argument("--ceiling", type=float)
    p.add_argument("--out")
    p.add_argument("--budget")
    p.add_argument("--model", action="append")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--probe", action="store_true", help="send a one-word sign-in check before the real question")
    p.add_argument("--no-probe", action="store_true", help=argparse.SUPPRESS)  # the default since 0.1.0
    p.add_argument("--raw", action="store_true")
    p.add_argument("--blind", action="store_true")
    p.add_argument("--session")
    p = sub.add_parser("new")
    p.add_argument("--mode", choices=list(CEILINGS), default="ask")
    p = sub.add_parser("last")
    p = sub.add_parser("budget")
    p.add_argument("mode", nargs="?")
    p = sub.add_parser("put")
    p.add_argument("run")
    p.add_argument("name")
    args = ap.parse_args(argv)
    if os.name != "posix":
        print("Sidecall v0.1 supports macOS and Linux only (process-group control).", file=sys.stderr)
        return 2
    reg = load_registry(args.registry)
    if not reg:
        print("registry.json is missing or unreadable.", file=sys.stderr)
        return 2
    guard_signals()  # before any child process exists
    try:
        return {"discover": cmd_discover, "probe": cmd_probe, "run": cmd_run, "last": cmd_last,
                "budget": cmd_budget, "put": cmd_put, "new": cmd_new}[args.cmd](args, reg)
    finally:
        stop_all()


if __name__ == "__main__":
    sys.exit(main())
