"""Offline tests for fanout.py: fake CLIs only, no network, no quota."""
import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import fanout  # noqa: E402

FAKE = str(ROOT / "tests" / "fakebin" / "fakecli.py")
REAL_REG = json.loads((ROOT / "scripts" / "registry.json").read_text())


def tool(mode, family, via="arg", models=None, required=False, scarce=False):
    argv = [sys.executable, FAKE, mode, "{model_args}", "{effort_args}"]
    if via == "arg":
        argv.append("{prompt}")
    elif via == "file":
        argv += ["--prompt-file", "{prompt_file}"]
    return {"family": family, "binary": sys.executable, "version_cmd": [sys.executable, "--version"],
            "argv": argv, "prompt_via": via, "model_flag": "-m", "model_required": required,
            "models": models if models is not None else {"standard": {"model": "std-1", "effort": "medium"},
                                                        "stronger": {"model": "str-1", "effort": "high"},
                                                        "strongest": "top-1"},
            "effort_args": ["--effort", "{effort}"],
            "default_on": True, "scarce_quota": scarce, "login_hint": "fake login",
            "quota_patterns": ["usage limit", "\\b403\\b"], "auth_patterns": ["not logged in"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.state = self.dir / "state"
        self.counter = self.dir / "calls.log"
        os.environ["FAKECLI_COUNTER"] = str(self.counter)

    def tearDown(self):
        os.environ.pop("FAKECLI_COUNTER", None)
        self.tmp.cleanup()

    def registry(self, tools):
        reg = {"default_budget": "standard", "budgets": REAL_REG["budgets"], "preference": list(tools),
               "max_panel": 4, "tools": tools, "suspect_patterns": REAL_REG["suspect_patterns"]}
        p = self.dir / "registry.json"
        p.write_text(json.dumps(reg))
        return str(p)

    def run_cli(self, tools, *extra, question="What is the capital of Australia?"):
        q = self.dir / "q.txt"
        if not q.exists():
            q.write_text(question)
        out = self.dir / ("run-%d" % len(list(self.dir.glob("run-*"))))
        argv = ["--state", str(self.state), "--registry", self.registry(tools), "run", "--question-file", str(q),
                "--out", str(out), "--no-probe", *extra]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fanout.main(argv)
        meta = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
        return code, buf.getvalue(), out, meta


class TestRun(Base):
    def test_ok_fast_and_explicit_model(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")})
        self.assertEqual(code, 0, text)
        self.assertEqual(meta["sources"]["a"]["status"], "ok")
        self.assertIn("-m std-1 --effort medium", (out / "answers" / "a.md").read_text())
        self.assertIn("a (std-1, medium (requested))", meta["run_line"])
        self.assertIn("Standard mode: a = std-1 (medium), b = std-1 (medium).", text)
        self.assertIn("uses your quota faster", text)
        self.assertIn("Standard mode:", meta["run_line"])

    def test_slow_tool_gives_partial_and_keeps_text(self):
        t0 = time.monotonic()
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "s": tool("slow", "xai")},
                                             "--ceiling", "2")
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(meta["sources"]["s"]["status"], "partial")
        self.assertIn("ceiling", meta["sources"]["s"]["flags"])
        self.assertIn("first part", (out / "answers" / "s.md").read_text())

    def test_stdin_is_closed_so_nothing_hangs(self):
        t0 = time.monotonic()
        code, text, out, meta = self.run_cli({"h": tool("stdin", "openai"), "a": tool("ok", "xai")},
                                             "--ceiling", "20")
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(meta["sources"]["h"]["status"], "ok")
        self.assertIn("0 bytes", (out / "answers" / "h.md").read_text())

    def test_no_orphans_after_ceiling(self):
        pidfile = self.dir / "child.pid"
        os.environ["FAKECLI_PIDFILE"] = str(pidfile)
        try:
            self.run_cli({"c": tool("child", "openai"), "a": tool("ok", "xai")}, "--ceiling", "2")
        finally:
            os.environ.pop("FAKECLI_PIDFILE", None)
        pid = int(pidfile.read_text())
        time.sleep(0.5)
        alive = True
        try:
            os.kill(pid, 0)
            # a zombie is not a survivor: check it is really gone or reaped
            with open(os.devnull, "w"):
                pass
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive:
            import subprocess
            st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
            alive = bool(st) and not st.startswith("Z")
        self.assertFalse(alive, "child process survived the ceiling")

    def test_helper_left_behind_by_a_finished_tool_is_stopped(self):
        pidfile = self.dir / "leaver.pid"
        os.environ["FAKECLI_PIDFILE"] = str(pidfile)
        try:
            code, text, out, meta = self.run_cli({"l": tool("leaver", "openai"), "a": tool("ok", "xai")})
        finally:
            os.environ.pop("FAKECLI_PIDFILE", None)
        self.assertEqual(meta["sources"]["l"]["status"], "ok")
        time.sleep(0.5)
        import subprocess
        st = subprocess.run(["ps", "-o", "stat=", "-p", pidfile.read_text()], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not st or st.startswith("Z"), "helper survived")

    def test_quota_and_auth_are_classified_with_fix_lines(self):
        code, text, out, meta = self.run_cli({"q": tool("quota", "openai"), "l": tool("auth", "xai"),
                                              "a": tool("ok", "google")})
        self.assertEqual(meta["sources"]["q"]["status"], "quota")
        self.assertEqual(meta["sources"]["l"]["status"], "not_logged_in")
        self.assertIn("fake login", text)
        self.assertIn("one opinion, not a comparison", text)
        self.assertEqual(code, 5)
        breaker = json.loads((self.state / "breaker.json").read_text())
        self.assertEqual(breaker["q"]["status"], "quota")
        self.assertGreater(breaker["q"]["until"] - breaker["q"]["at"], 5 * 3600)

    def test_huge_output_is_capped(self):
        code, text, out, meta = self.run_cli({"h": tool("huge", "openai"), "a": tool("ok", "xai")},
                                             "--ceiling", "20")
        self.assertIn("capped", meta["sources"]["h"]["flags"])
        self.assertLessEqual((out / "answers" / "h.md").stat().st_size, fanout.OUTPUT_CAP + 2)

    def test_large_prompt_via_stdin_arrives_intact(self):
        q = self.dir / "q.txt"
        q.write_text("Summarize this. " + ("lorem ipsum dolor sit amet " * 11000))
        code, text, out, meta = self.run_cli({"s": tool("sha", "openai", via="stdin"), "a": tool("ok", "xai")},
                                             "--raw")
        import hashlib
        want = hashlib.sha256((out / "prompt.md").read_bytes()).hexdigest()
        self.assertGreater((out / "prompt.md").stat().st_size, 290000)
        self.assertIn(want, (out / "answers" / "s.md").read_text())

    def test_prompt_file_delivery(self):
        code, text, out, meta = self.run_cli({"f": tool("file", "openai", via="file"), "a": tool("ok", "xai")})
        self.assertEqual(meta["sources"]["f"]["status"], "ok")
        self.assertIn("capital of Australia", (out / "answers" / "f.md").read_text())

    def test_secret_in_prompt_is_refused(self):
        q = self.dir / "q.txt"
        q.write_text("Why does this fail? key = AKIAABCDEFGHIJKLMNOP")
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")})
        self.assertEqual(code, 3)
        self.assertIn("REFUSED", text)
        self.assertNotIn("AKIAABCDEFGHIJKLMNOP", text)
        self.assertFalse(self.counter.exists(), "a tool was called despite the secret")

    def test_api_key_env_is_stripped(self):
        os.environ["OPENAI_API_KEY"] = "sk-test-should-not-leak-000000000000"
        try:
            code, text, out, meta = self.run_cli({"e": tool("env", "openai"), "a": tool("ok", "xai")})
        finally:
            os.environ.pop("OPENAI_API_KEY", None)
        self.assertIn("OPENAI_API_KEY=EMPTY", (out / "answers" / "e.md").read_text())
        self.assertIn("OPENAI_API_KEY", meta["env_stripped"])
        self.assertNotIn("sk-test", json.dumps(meta))

    def test_empty_answer_is_suspect_and_excluded(self):
        code, text, out, meta = self.run_cli({"x": tool("empty", "xai"), "a": tool("ok", "openai")})
        self.assertEqual(meta["sources"]["x"]["status"], "suspect")
        self.assertEqual(code, 5)

    def test_fewer_than_two_tools_is_not_a_comparison(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai")})
        self.assertIn("one opinion, not a comparison", text)
        self.assertEqual(code, 5)

    def test_panel_is_one_per_family(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "a2": tool("ok2", "openai"),
                                              "b": tool("ok2", "xai")})
        self.assertNotIn("a2", meta["sources"])

    def test_blind_labels_and_map(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")}, "--blind")
        mapping = json.loads((out / "blind" / "map.json").read_text())
        self.assertEqual(sorted(mapping.values()), ["a", "b"])
        for label, name in mapping.items():
            self.assertEqual((out / "blind" / f"{label}.md").read_text(), (out / "answers" / f"{name}.md").read_text())


class TestModels(Base):
    def test_budget_maps_to_registry_ids_and_override_wins(self):
        reg = REAL_REG
        spec = reg["tools"]["codex"]
        for budget in ("standard", "stronger", "strongest"):
            m, e, _ = fanout.choice_for("codex", spec, {"tools": {}}, budget, {})
            self.assertEqual(m, spec["models"][budget]["model"])
            self.assertEqual(e, spec["models"][budget]["effort"])
        mine = {"tools": {"codex": {"model": "mine"}}}
        self.assertEqual(fanout.choice_for("codex", spec, mine, "standard", {})[0], "mine")
        self.assertEqual(fanout.choice_for("codex", spec, mine, "standard", {"codex": "once"})[0], "once")

    def test_default_is_standard_with_the_owner_ids(self):
        self.assertEqual(REAL_REG["default_budget"], "standard")
        t = REAL_REG["tools"]
        self.assertEqual(t["codex"]["models"]["standard"], {"model": "gpt-5.6-luna", "effort": "medium"})
        self.assertEqual(t["claude"]["models"]["standard"], {"model": "sonnet", "effort": "medium"})
        self.assertEqual(t["grok"]["models"]["standard"]["effort"], "medium")
        self.assertIn("flash", t["agy"]["models"]["standard"]["model"])

    def test_codex_argv_always_has_explicit_model_and_effort(self):
        spec = REAL_REG["tools"]["codex"]
        for budget in ("standard", "stronger", "strongest"):
            m, e, _ = fanout.choice_for("codex", spec, {"tools": {}}, budget, {})
            argv = fanout.build_argv(spec, m, "q", "p.md", "f.md", e)
            self.assertIn("-m", argv)
            self.assertEqual(argv[argv.index("-m") + 1], spec["models"][budget]["model"])
            self.assertIn(f'model_reasoning_effort="{spec["models"][budget]["effort"]}"', argv)
            self.assertEqual(argv[:2], ["codex", "exec"])
            self.assertIn("read-only", argv)
            self.assertEqual(argv[-1], "-")
        m, e, _ = fanout.choice_for("codex", spec, {"tools": {}}, "standard", {})
        self.assertEqual(fanout.build_argv(spec, m, "q", "p", "f", e)[5:9],
                         ["-m", "gpt-5.6-luna", "-c", 'model_reasoning_effort="medium"'])

    def test_notice_names_every_model(self):
        n = fanout.mode_notice(REAL_REG, {"tools": {}}, "standard", ["codex", "claude", "agy", "grok"], {})
        self.assertTrue(n.startswith("Standard mode: codex = gpt-5.6-luna (medium), claude = sonnet (medium)"))
        self.assertIn("--budget stronger", n)
        self.assertIn("uses your quota faster", n)
        self.assertIn("uses your quota faster", fanout.mode_notice(REAL_REG, {"tools": {}}, "strongest", ["codex"], {}))

    def test_model_required_without_model_is_skipped(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai", models={}, required=True),
                                              "b": tool("ok2", "xai")})
        self.assertEqual(meta["sources"]["a"]["status"], "skipped")
        self.assertIn("hidden default", meta["sources"]["a"]["note"])

    def test_budget_flag_changes_model(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")},
                                             "--budget", "stronger", "--model", "b=custom-9")
        self.assertEqual(meta["sources"]["a"]["model"], "str-1, high (requested)")
        self.assertEqual(meta["sources"]["a"]["model_requested"], "str-1")
        self.assertEqual(meta["sources"]["a"]["effort"], "high")
        self.assertEqual(meta["sources"]["b"]["model_requested"], "custom-9")
        self.assertEqual(meta["sources"]["b"]["model"], "custom-9, high (requested)")


class TestProbeAndBreaker(Base):
    def test_breaker_suppresses_repeated_probe(self):
        reg = json.loads(pathlib.Path(self.registry({"a": tool("ok", "openai")})).read_text())
        user = {"tools": {}}
        r1 = fanout.probe(reg, user, self.state, ["a"], "standard", {})
        r2 = fanout.probe(reg, user, self.state, ["a"], "standard", {})
        self.assertEqual(r1["a"]["status"], "live")
        self.assertIn("cached", r2["a"]["why"])
        self.assertEqual(self.counter.read_text().count("ok"), 1)

    def test_scarce_tool_is_never_probed(self):
        reg = json.loads(pathlib.Path(self.registry({"s": tool("ok", "google", scarce=True)})).read_text())
        r = fanout.probe(reg, {"tools": {}}, self.state, ["s"], "standard", {})
        self.assertEqual(r["s"]["status"], "unprobed")
        self.assertFalse(self.counter.exists())

    def test_open_breaker_skips_tool_in_run(self):
        fanout.breaker_set(self.state, "q", "quota", "weekly limit")
        code, text, out, meta = self.run_cli({"q": tool("ok", "openai"), "a": tool("ok2", "xai"),
                                              "b": tool("ok", "google")})
        self.assertEqual(meta["sources"]["q"]["status"], "skipped")
        self.assertNotIn("q", [l.split()[0] for l in self.counter.read_text().splitlines() if l == "q"])

    def test_dry_run_spends_nothing(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")}, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", text)
        self.assertFalse(self.counter.exists())


class TestPut(Base):
    def test_put_only_known_names_in_run_folder(self):
        run = self.dir / "r"
        run.mkdir()
        (run / "prompt.md").write_text("q")
        with self.assertRaises(SystemExit):
            fanout.main(["put", str(run), "../evil.sh"])
        old = sys.stdin
        sys.stdin = io.StringIO('{"claims": []}')
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                fanout.main(["put", str(run), "claims.json"])
        finally:
            sys.stdin = old
        self.assertTrue((run / "claims.json").exists())


class TestModes(Base):
    def test_critic_embeds_subject_and_excludes_families(self):
        subj = self.dir / "change.diff"
        subj.write_text("-    for i in range(n):\n+    for i in range(n + 1):\n")
        code, text, out, meta = self.run_cli({"c": tool("ok", "anthropic"), "o": tool("ok", "openai"),
                                              "x": tool("ok2", "xai")},
                                             "--mode", "critic", "--subject-file", str(subj), "--pick", "1",
                                             "--exclude-family", "anthropic,openai")
        prompt = (out / "prompt.md").read_text()
        self.assertIn("range(n + 1)", prompt)
        self.assertIn("Assume it contains at least one real mistake", prompt)
        self.assertEqual([n for n, s in meta["sources"].items() if s["status"] == "ok"], ["x"])
        self.assertEqual(code, 0)

    def test_second_picks_one_tool(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")},
                                             "--mode", "second", "--pick", "1", "--exclude-family", "anthropic")
        self.assertEqual(code, 0)
        self.assertEqual([n for n, s in meta["sources"].items() if s["status"] == "ok"], ["a"])

    def test_discover_and_last(self):
        reg = self.registry({"a": tool("ok", "openai")})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fanout.main(["--state", str(self.state), "--registry", reg, "discover", "--write"])
        self.assertIn("Standard mode: a = std-1 (medium)", buf.getvalue())
        self.assertTrue((self.state / "tools.json").exists())
        self.assertFalse(self.counter.exists(), "discover must not call the model")
        self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")})
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fanout.main(["--state", str(self.state), "--registry", reg, "last"])
        self.assertIn("Last run:", buf.getvalue())
        self.assertIn("Asked a (std-1, medium (requested))", buf.getvalue())


class TestInterrupt(Base):
    def test_killing_fanout_stops_its_tools(self):
        import signal
        import subprocess
        pidfile = self.dir / "child.pid"
        reg = self.registry({"c": tool("child", "openai"), "a": tool("slow", "xai")})
        q = self.dir / "q.txt"
        q.write_text("question text for the interrupt test")
        env = dict(os.environ, FAKECLI_PIDFILE=str(pidfile))
        p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fanout.py"), "--state", str(self.state),
                              "--registry", reg, "run", "--question-file", str(q), "--out", str(self.dir / "r"),
                              "--no-probe", "--ceiling", "60"], env=env, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        time.sleep(0.5)
        pid = int(pidfile.read_text())
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not st or st.startswith("Z"), "a tool's child survived after fanout was stopped")


class TestFixRound1(Base):
    """Fix round 1: S1 no hidden probes, S2 no orphans during a probe, S3 classification, S5 no escape hatches,
    S7 model defaults, S8 plain messages."""

    def test_run_sends_no_probe_unless_asked(self):
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")})
        self.assertEqual(self.counter.read_text().splitlines().count("ok"), 1, "one real call, no probe")
        self.assertNotIn("Checking sign-in", text)

    def test_probe_flag_announces_text_after_the_notice(self):
        q = self.dir / "q.txt"
        q.write_text("What is the capital of Australia?")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fanout.main(["--state", str(self.state), "--registry", self.registry({"a": tool("ok", "openai"),
                         "b": tool("ok2", "xai")}), "run", "--question-file", str(q), "--out", str(self.dir / "r"),
                         "--probe"])
        text = buf.getvalue()
        self.assertLess(text.index("Standard mode:"), text.index("Checking sign-in first"))
        self.assertIn(fanout.PROBE_PROMPT, text)
        self.assertEqual(self.counter.read_text().splitlines().count("ok"), 2, "probe + real call")

    def test_sigterm_during_probe_leaves_no_orphan(self):
        import signal
        import subprocess
        pidfile = self.dir / "child.pid"
        reg = self.registry({"c": tool("child", "openai")})
        env = dict(os.environ, FAKECLI_PIDFILE=str(pidfile))
        p = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "fanout.py"), "--state", str(self.state),
                              "--registry", reg, "probe", "--tools", "c"], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            time.sleep(0.1)
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=15)
        time.sleep(0.5)
        st = subprocess.run(["ps", "-o", "stat=", "-p", pidfile.read_text()], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not st or st.startswith("Z"), "a probe's child survived after fanout was stopped")

    def test_answer_mentioning_rate_limit_stays_ok(self):
        code, text, out, meta = self.run_cli({"r": tool("ratelimit", "openai"), "a": tool("ok", "xai")})
        self.assertEqual(meta["sources"]["r"]["status"], "ok")
        breaker = json.loads((self.state / "breaker.json").read_text())
        self.assertEqual(breaker["r"]["status"], "live")

    def test_no_escape_hatches(self):
        args = fanout.main.__code__.co_consts
        self.assertFalse(any("allow-secrets" in str(c) for c in args))
        os.environ["GITHUB_PAT"] = "x" * 30
        os.environ["DB_PASSWORD"] = "hunter2"
        os.environ["PRIVATE_KEY"] = "k"
        try:
            env, stripped = fanout.child_env({"pass_env": ["DB_PASSWORD"]})
        finally:
            for k in ("GITHUB_PAT", "DB_PASSWORD", "PRIVATE_KEY"):
                os.environ.pop(k, None)
        for k in ("GITHUB_PAT", "DB_PASSWORD", "PRIVATE_KEY"):
            self.assertNotIn(k, env)
            self.assertIn(k, stripped)
        self.assertIn("PATH", env)

    def test_secret_refusal_names_the_line(self):
        q = self.dir / "q.txt"
        q.write_text("line one\nmy token ghp_" + "a" * 36 + "\n")
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai"), "b": tool("ok2", "xai")})
        self.assertEqual(code, 3)
        self.assertIn("line 2", text)
        self.assertNotIn("allow-secrets", text)

    def test_critic_defaults_one_tier_up(self):
        subj = self.dir / "s.txt"
        subj.write_text("def f(n): return n + 1")
        code, text, out, meta = self.run_cli({"x": tool("ok2", "xai")}, "--mode", "critic",
                                             "--subject-file", str(subj))
        self.assertIn("Critic uses one tier up by default: standard -> stronger", text)
        self.assertEqual(meta["sources"]["x"]["model_requested"], "str-1")
        code, text, out, meta = self.run_cli({"x": tool("ok2", "xai")}, "--mode", "critic",
                                             "--subject-file", str(subj), "--budget", "standard")
        self.assertEqual(meta["sources"]["x"]["model_requested"], "std-1")

    def test_inherit_passes_no_model_flag_but_overrides_win(self):
        spec = REAL_REG["tools"]["codex"]
        m, e, src = fanout.choice_for("codex", spec, {"tools": {}}, "inherit", {})
        self.assertIsNone(m)
        self.assertNotIn("-m", fanout.build_argv(spec, m, "q", "p", "f", e))
        self.assertEqual(fanout.choice_for("codex", spec, {"tools": {"codex": {"model": "pin"}}}, "inherit", {})[0], "pin")
        self.assertEqual(fanout.choice_for("codex", spec, {"tools": {"codex": {"model": "pin"}}}, "inherit",
                                           {"codex": "flag"})[0], "flag")
        n = fanout.mode_notice(REAL_REG, {"tools": {}}, "inherit", ["codex"], {})
        self.assertIn("using your own CLI settings", n)
        self.assertIn("codex = vendor default (may be its most expensive model)", n)
        code, text, out, meta = self.run_cli({"a": tool("ok", "openai", required=True), "b": tool("ok2", "xai")},
                                             "--budget", "inherit")
        self.assertEqual(meta["sources"]["a"]["status"], "ok")
        self.assertNotIn("-m", (out / "answers" / "a.md").read_text())
        self.assertIn("vendor default", meta["sources"]["a"]["model"])

    def test_full_notice_first_then_short_until_mode_changes(self):
        tools = {"a": tool("ok", "openai"), "b": tool("ok2", "xai")}
        _, first, _, _ = self.run_cli(tools)
        _, second, _, _ = self.run_cli(tools)
        _, third, _, _ = self.run_cli(tools, "--budget", "stronger")
        self.assertIn("You can strengthen", first)
        self.assertIn("Standard mode: a = std-1 (medium), b = std-1 (medium).", second)
        self.assertNotIn("You can strengthen", second)
        self.assertIn("Stronger mode:", third)
        self.assertIn("You can strengthen", third)

    def test_reported_model_is_recorded(self):
        spec = tool("header", "openai")
        spec["report_patterns"] = {"model": "^model:\\s*(\\S+)", "effort": "^reasoning effort:\\s*(\\S+)"}
        code, text, out, meta = self.run_cli({"h": spec, "a": tool("ok", "xai")})
        self.assertEqual(meta["sources"]["h"]["model"], "real-model-7, low (reported by the tool)")

    def test_critic_without_subject_names_its_flags(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit) as cm:
            fanout.main(["--state", str(self.state), "--registry", self.registry({"a": tool("ok", "openai")}),
                         "run", "--mode", "critic", "--out", str(self.dir / "r")])
        self.assertIn("--subject-file", str(cm.exception.code))

    def test_missing_git_is_a_plain_message(self):
        old = os.environ["PATH"]
        os.environ["PATH"] = str(self.dir)
        try:
            with self.assertRaises(SystemExit) as cm:
                fanout.main(["--state", str(self.state), "--registry", self.registry({"a": tool("ok", "openai")}),
                             "run", "--mode", "critic", "--subject-diff", "--out", str(self.dir / "r")])
        finally:
            os.environ["PATH"] = old
        self.assertIn("Git is not installed", str(cm.exception.code))

    def test_new_makes_a_run_folder(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fanout.main(["--state", str(self.state), "new", "--mode", "ask"])
        run = pathlib.Path(buf.getvalue().strip().splitlines()[-1].split("=", 1)[1])
        self.assertTrue(run.is_dir())
        self.assertEqual(list(run.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
