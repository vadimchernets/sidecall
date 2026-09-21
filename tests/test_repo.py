"""Repository rules (stdlib only, frontmatter, no Cyrillic, deny-list) run as a test."""
import contextlib
import io
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import lint_repo  # noqa: E402


class TestRepo(unittest.TestCase):
    def test_lint_repo_is_clean(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = lint_repo.main([])
        self.assertEqual(code, 0, buf.getvalue())


if __name__ == "__main__":
    unittest.main()
