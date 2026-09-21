#!/usr/bin/env python3
"""lint_repo.py: repository rules that tests cannot see in behaviour.

Checks: every Python import is from the standard library (3.9+); every SKILL.md has frontmatter with a name and
a description; no Cyrillic character anywhere in the repository; no name from the deny-list of other projects in
README, skills or scripts; manifests carry version 0.1.0, Apache-2.0 and the author. Optional: --shared DIR checks that
merge.py and families.json are byte-identical to the canonical copies.

Usage: python3 tools/lint_repo.py [--shared DIR]      exit 0 clean, 1 problems (listed)
"""
import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "argparse", "ast", "concurrent", "contextlib", "datetime", "hashlib", "io", "json", "os", "pathlib",
    "random", "re", "shutil", "signal", "subprocess", "sys", "tempfile", "time", "unicodedata", "unittest"}
LOCAL = {"fanout", "merge", "lint_repo"}
# Names that must not appear in user-facing text (other projects, private names) are kept in a local file that is
# not part of the repository; without it this check is skipped.
DENY_FILE = ROOT / "tools" / "deny.local.txt"
DENY = ([ln.strip() for ln in DENY_FILE.read_text(encoding="utf-8").splitlines()
         if ln.strip() and not ln.startswith("#")] if DENY_FILE.exists() else [])
CYRILLIC = re.compile(r"[\u0400-\u04ff\u0500-\u052f]")
TEXT_EXT = {".py", ".md", ".json", ".txt", ".cff", ""}


def files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            yield p


def main(argv):
    problems = []
    for p in files():
        rel = p.relative_to(ROOT)
        if p.suffix not in TEXT_EXT:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if CYRILLIC.search(text):
            problems.append(f"{rel}: contains Cyrillic characters")
        if p.suffix == ".py":
            try:
                tree = ast.parse(text)
            except SyntaxError as e:
                problems.append(f"{rel}: syntax error {e}")
                continue
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module.split(".")[0]]
                for n in names:
                    if n not in STDLIB and n not in LOCAL:
                        problems.append(f"{rel}: non-stdlib import '{n}'")
        if rel.parts[0] in ("skills", "scripts") or rel.name == "README.md":
            if rel.name == "lint_repo.py":
                continue
            low = text.lower()
            for pat in DENY:
                if re.search(pat, low):
                    problems.append(f"{rel}: names another project ({pat})")
    for skill in sorted((ROOT / "skills").glob("*/SKILL.md")):
        text = skill.read_text(encoding="utf-8")
        m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if not m:
            problems.append(f"{skill.relative_to(ROOT)}: no frontmatter")
            continue
        fm = m.group(1)
        for key in ("name", "description"):
            if not re.search(rf"^{key}:\s*\S", fm, re.M):
                problems.append(f"{skill.relative_to(ROOT)}: frontmatter lacks {key}")
    plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())
    if plugin.get("version") != "0.1.0" or plugin.get("license") != "Apache-2.0" or \
            (plugin.get("author") or {}).get("name") != "Vadym Chernets":
        problems.append("plugin.json: version 0.1.0, license Apache-2.0 and author Vadym Chernets are required")
    if "--shared" in argv:
        shared = pathlib.Path(argv[argv.index("--shared") + 1])
        for name in ("merge.py", "families.json"):
            if (shared / name).read_bytes() != (ROOT / "scripts" / name).read_bytes():
                problems.append(f"scripts/{name} differs from the canonical copy in {shared}")
    for p in problems:
        print(p)
    print("lint_repo: " + ("clean" if not problems else f"{len(problems)} problem(s)"))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
