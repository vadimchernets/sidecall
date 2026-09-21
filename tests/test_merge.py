"""Shared merge suite: the same fixtures must pass in every repository that vendors merge.py."""
import contextlib
import io
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import merge  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def quiet(fn, *a):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
        code = fn(*a)
    return code, buf.getvalue()


class MergeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self.tmp.name) / "run"
        shutil.copytree(FIX / "run-basic", self.run_dir)
        self.fam = merge.load_families()

    def tearDown(self):
        self.tmp.cleanup()

    def report(self):
        code, _ = quiet(merge.main, ["render", str(self.run_dir)])
        self.assertEqual(code, 0)
        return (self.run_dir / "report.md").read_text()


class TestValidate(MergeCase):
    def test_fabricated_quote_is_rejected(self):
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        res = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(res["ok"])
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("c5", res["errors"][0])
        self.assertEqual(res["claims_ok"], 4)

    def test_markdown_marks_and_ellipsis_still_match(self):
        n = merge.norm((self.run_dir / "answers" / "codex.md").read_text())
        self.assertTrue(merge.find_quote("The Parliament first sat in Canberra in 1927", n)[0])
        a = merge.norm((self.run_dir / "answers" / "agy.md").read_text())
        self.assertTrue(merge.find_quote("designed by Walter Burley Griffin ... Marion Mahony Griffin", a)[0])
        self.assertFalse(merge.find_quote("Marion Mahony Griffin ... designed by Walter", a)[0])
        self.assertFalse(merge.find_quote("Canberra", a)[0], "a too-short quote proves nothing")

    def test_injection_is_flagged_not_obeyed(self):
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        self.assertIn("agy", json.loads(out)["injection_suspect"])
        self.assertIn("treated as data and not followed", self.report())

    def test_omission_backstop_lists_uncovered_sentences(self):
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        missed = json.loads(out)["possibly_missed"]
        self.assertIn("codex", missed)
        self.assertTrue(any("Sydney and Melbourne" in s for s in missed["codex"]))
        self.assertNotIn("copilot", missed, "a 'skipped' entry counts as addressed")

    def test_source_without_usable_answer_cannot_support(self):
        claims = json.loads((self.run_dir / "claims.json").read_text())
        claims["claims"].append({"id": "c9", "text": "x", "support": [{"source": "deepseek",
                                                                        "quote": "anything at all here"}]})
        (self.run_dir / "claims.json").write_text(json.dumps(claims))
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        self.assertTrue(any("deepseek" in e and "no usable answer" in e for e in json.loads(out)["errors"]))


class TestRender(MergeCase):
    def test_sections_and_classification(self):
        r = self.report()
        agreed = r.split("## Agreed across families")[1].split("## ")[0]
        disputed = r.split("## Disputed")[1].split("## Echo")[0]
        echo = r.split("## Echo")[1].split("## Single-source")[0]
        single = r.split("## Single-source")[1].split("## ")[0]
        self.assertIn("**c1**", agreed)
        self.assertIn("**c2**", disputed)
        self.assertIn("1929", disputed)
        self.assertIn("does not pick a winner", disputed)
        self.assertIn("**c3**", echo)
        self.assertIn("**c4**", single)
        self.assertIn("## Could not be verified", r)
        self.assertIn("**c5**", r.split("## Could not be verified")[1])

    def test_independence_line_says_assumed(self):
        r = self.report()
        self.assertIn("independent voices under assumed correlations", r)
        self.assertIn("https://github.com/vadimchernets/after-chat", r)
        self.assertIn("assumptions, not measurements", r)

    def test_who_answered_lists_failures(self):
        r = self.report()
        self.assertRegex(r, r"\| deepseek \| deepseek \|.*\| quota")
        self.assertRegex(r, r"\| grok \| xai \|.*\| partial \(ceiling\)")

    def test_one_answer_banner(self):
        run = pathlib.Path(self.tmp.name) / "one"
        shutil.copytree(FIX / "run-one", run)
        quiet(merge.main, ["render", str(run)])
        r = (run / "report.md").read_text()
        self.assertIn("This is one opinion, not a comparison", r)
        self.assertNotIn("## Agreed across families", r)

    def test_blind_labels_are_translated(self):
        (self.run_dir / "blind").mkdir()
        mapping = {"A": "codex", "B": "grok"}
        for k, v in mapping.items():
            shutil.copyfile(self.run_dir / "answers" / f"{v}.md", self.run_dir / "blind" / f"{k}.md")
        (self.run_dir / "blind" / "map.json").write_text(json.dumps(mapping))
        (self.run_dir / "claims.json").write_text(json.dumps({"claims": [
            {"id": "c1", "text": "Canberra is the capital.", "support": [
                {"source": "A", "quote": "Canberra is the capital of Australia."},
                {"source": "B", "quote": "Canberra is the capital of Australia."}]}]}))
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        self.assertEqual(code, 0, out)
        self.assertIn("[codex, grok]", self.report())

    def test_own_view_from_own_md(self):
        (self.run_dir / "own.md").write_text("My own draft answer: Canberra, 1927.")
        r = self.report()
        self.assertIn("Drafted before reading the others", r)
        self.assertIn("My own draft answer", r.split("## Claude's own view")[1])


class TestLint(MergeCase):
    def fill(self, bullets):
        r = self.report().replace(merge.PLACEHOLDER_BOTTOM, "\n".join(bullets))
        (self.run_dir / "report.md").write_text(r)
        code, out = quiet(merge.main, ["lint", str(self.run_dir)])
        return code, json.loads(out)

    def test_placeholder_fails(self):
        self.report()
        code, out = quiet(merge.main, ["lint", str(self.run_dir)])
        self.assertEqual(code, 1)

    def test_tagged_bullets_pass(self):
        code, res = self.fill(["- Canberra is the capital. [codex, grok, agy]",
                               "- The year Parliament first sat there is disputed. [codex, grok]"])
        self.assertEqual(code, 0, res)

    def test_untagged_bullet_fails(self):
        code, res = self.fill(["- Canberra is the capital."])
        self.assertEqual(code, 1)
        self.assertIn("untagged", res["problems"][0])

    def test_tag_naming_a_silent_source_fails(self):
        code, res = self.fill(["- Canberra is the capital. [codex, deepseek]"])
        self.assertEqual(code, 1)
        self.assertIn("deepseek", res["problems"][0])

    def test_more_than_five_bullets_fails(self):
        code, res = self.fill([f"- point {i}. [codex]" for i in range(6)])
        self.assertEqual(code, 1)


class TestVoices(unittest.TestCase):
    def setUp(self):
        self.fam = merge.load_families()

    def test_assumed_flag_and_defaults(self):
        self.assertTrue(self.fam["assumed"])
        self.assertEqual(self.fam["rho"], {"same_family": 0.9, "same_cluster": 0.6, "other": 0.35})

    def test_four_cn_cluster_sources_are_about_1_4(self):
        self.assertAlmostEqual(merge.voices(["deepseek", "qwen", "kimi", "glm"], self.fam), 1.43, places=2)

    def test_three_cross_family_sources_are_about_1_8(self):
        self.assertAlmostEqual(merge.voices(["codex", "agy", "grok"], self.fam), 1.76, places=2)

    def test_same_family_is_about_one_voice(self):
        self.assertAlmostEqual(merge.voices(["chatgpt", "copilot"], self.fam), 2 / 1.9, places=3)

    def test_uniform_rho_reduces_to_textbook_formula(self):
        n, r = 5, self.fam["rho"]["other"]
        v = merge.voices(["codex", "agy", "grok", "claude", "mistral"], self.fam)
        self.assertAlmostEqual(v, n / (1 + (n - 1) * r), places=6)


class TestClusterAndEllipsis(unittest.TestCase):
    """M1: agreement between different families of one cluster is not called one family.
    M2: an ellipsis may shorten a sentence but never join words from far apart."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.run_dir = pathlib.Path(self.tmp.name) / "run"
        shutil.copytree(FIX / "run-cluster", self.run_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_same_cluster_different_families_is_not_echo(self):
        quiet(merge.main, ["render", str(self.run_dir)])
        r = (self.run_dir / "report.md").read_text()
        self.assertIn("## Agreement within one cluster (assumed correlation 0.6)", r)
        section = r.split("## Agreement within one cluster")[1].split("## ")[0]
        self.assertIn("**c1**", section)
        self.assertNotIn("## Echo", r)
        self.assertNotIn("## Agreed across families", r)

    def test_ellipsis_cannot_join_distant_pieces(self):
        code, out = quiet(merge.main, ["validate", str(self.run_dir)])
        errors = json.loads(out)["errors"]
        self.assertEqual(code, 1)
        self.assertTrue(any(e.startswith("c2") and "ellipsis skips too much" in e for e in errors), errors)
        self.assertFalse(any(e.startswith("c3") for e in errors), "a short skip inside one sentence is fine")

    def test_later_occurrence_of_first_piece_can_satisfy_the_window(self):
        text = merge.norm("Paris is big. " + "filler words here. " * 10 + "Paris is the capital of France.")
        self.assertTrue(merge.find_quote("Paris is ... capital of France", text)[0])


if __name__ == "__main__":
    unittest.main()
