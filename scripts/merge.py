#!/usr/bin/env python3
"""merge.py: prove the claims, weigh the families, render the report.

The model reads the saved answers and writes claims.json: each claim carries a verbatim quote from
every source that supports it (and from every source that contradicts it). This script does the rest
deterministically: it rejects any quote that is not in that source's saved answer, groups sources by
model family, classifies each claim (agreed across families, echo inside one family, disputed,
single-source), and writes report.md. The model then adds only the Bottom line and its own view, and
`lint` checks that every Bottom-line bullet names the sources it rests on.

Run folder contract (see claims-format.md):
    <run>/answers/<source>.md   one saved answer per source, verbatim
    <run>/meta.json             question + per-source status (optional; inferred from answers/ if absent)
    <run>/claims.json           written by the model
    <run>/own.md                optional: the conductor's own answer, drafted before reading the others

Commands:
    merge.py validate RUN [--claims FILE] [--map FILE] [--families FILE]
    merge.py render   RUN [--claims FILE] [--map FILE] [--families FILE] [--out FILE]
    merge.py lint     RUN [--report FILE]
    merge.py voices   SOURCE [SOURCE ...] [--families FILE]

Exit codes: 0 fine, 1 problems found (listed), 2 usage or input error.
Python 3.9+, standard library only. Apache-2.0 licence, Vadym Chernets 2026.
"""
import argparse
import json
import pathlib
import re
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
USABLE = ("ok", "partial")
KINDS = ("fact", "number", "advice")
MIN_QUOTE_CHARS = 10
MAX_ELLIPSIS_GAP = 80  # normalized characters a '...' may skip between two pieces of one quote
MAX_MISSED_PER_SOURCE = 12
MAX_NUMBER_PAIRS = 10
PLACEHOLDER_BOTTOM = "<!-- bottom-line: up to 5 bullets, each ending with [source, source] -->"
PLACEHOLDER_OWN = "<!-- own-view: written last, labelled, never counted -->"

_TRANS = {
    0x2018: "'", 0x2019: "'", 0x201A: "'", 0x201B: "'", 0x2032: "'",
    0x201C: '"', 0x201D: '"', 0x201E: '"', 0x2033: '"', 0x00AB: '"', 0x00BB: '"',
    0x2010: "-", 0x2011: "-", 0x2012: "-", 0x2013: "-", 0x2014: "-", 0x2015: "-", 0x2212: "-",
    0x00A0: " ", 0x202F: " ", 0x2009: " ", 0x200B: "",
}
_MARKS = re.compile(r"[*_`#>|~\[\]]")
_SPACE = re.compile(r"\s+")
_ELLIPSIS = re.compile(r"\.\.\.+")
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[\"'(\[]?[A-Z0-9])")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?%?")
_PROPER = re.compile(r"(?<!^)(?<![.!?]\s)\b[A-Z][a-z]{2,}")
_WORD = re.compile(r"[^\W\d_]{4,}")
_STOP = set("""about above after again against also among another because been before being below between
both could does doing down during each even every from further have having here into itself just last
least less like made make many more most much must near never next only other over same should since
some such than that their them then there these they this those through under until very were what
when where which while will with within without would your year years first second third also often
usually however therefore answer question""".split())
_INJECTION = [
    re.compile(p, re.I) for p in (
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|messages|prompts?)",
        r"\bdisregard\s+(?:all\s+|any\s+|your\s+|the\s+)?(?:previous\s+|prior\s+|above\s+)?instructions",
        r"\b(?:new|updated|hidden)\s+instructions\s+for\s+(?:the\s+)?(?:ai|assistant|agent|model|claude)",
        r"\byou\s+are\s+now\s+(?:a|an|the|in)\b",
        r"\b(?:assistant|claude|ai\s+agent|the\s+agent)\s*[,:]?\s+(?:must|should)\s+(?:now\s+)?"
        r"(?:run|execute|delete|send|upload|open|download)\b",
        r"\breveal\s+(?:your|the)\s+system\s+prompt",
        r"\bcurl\b[^\n|]{0,200}\|\s*(?:ba|z)?sh\b",
        r"\brm\s+-rf\s+[/~]",
    )
]


# ---------------------------------------------------------------- text helpers

def norm(text):
    """Casefold, unify quotes and dashes, drop markdown marks, collapse whitespace."""
    text = unicodedata.normalize("NFKC", text or "").translate(_TRANS)
    text = _MARKS.sub(" ", text)
    return _SPACE.sub(" ", text).strip().casefold()


def quote_fragments(quote):
    """A quote may skip words with '...'; each fragment must appear, in order."""
    q = norm(quote).strip(" \"'")
    return [f.strip(" \"'") for f in _ELLIPSIS.split(q) if f.strip(" \"'")]


def find_quote(quote, haystack_norm):
    """Return (True, '') when the quote is in the normalized text, else (False, reason).

    Pieces separated by '...' must appear in order, each starting within MAX_ELLIPSIS_GAP characters of the end
    of the previous piece, so an ellipsis cannot stitch together words from unrelated sentences.
    """
    frags = quote_fragments(quote)
    if not frags:
        return False, "empty quote"
    if sum(len(f) for f in frags) < MIN_QUOTE_CHARS:
        return False, f"quote too short (under {MIN_QUOTE_CHARS} characters)"
    start = haystack_norm.find(frags[0])
    if start < 0:
        return False, "quote not found verbatim in the saved answer"
    too_far = False
    while start >= 0:
        pos, ok = start + len(frags[0]), True
        for frag in frags[1:]:
            at = haystack_norm.find(frag, pos)
            if at < 0:
                ok = False
                break
            if at - pos > MAX_ELLIPSIS_GAP:
                ok, too_far = False, True
                break
            pos = at + len(frag)
        if ok:
            return True, ""
        start = haystack_norm.find(frags[0], start + 1)
    if too_far:
        return False, f"ellipsis skips too much (over {MAX_ELLIPSIS_GAP} characters between pieces)"
    return False, "quote not found verbatim in the saved answer"


def sentences(text):
    out = []
    for block in re.split(r"\n\s*\n|\n", text or ""):
        block = block.strip().lstrip("-*+>#| \t").strip()
        block = re.sub(r"^\d+[.)]\s+", "", block)
        if not block:
            continue
        for s in _SENT_SPLIT.split(block):
            s = s.strip()
            if len(s.split()) >= 4:
                out.append(s)
    return out


def paragraph_with(text, quote):
    """The paragraph of `text` that holds the first fragment of `quote` (for the minority report)."""
    frags = quote_fragments(quote)
    if not frags:
        return ""
    for para in re.split(r"\n\s*\n", text or ""):
        if frags[0] in norm(para):
            return para.strip()
    return ""


def keywords(sentence):
    return {w for w in (m.casefold() for m in _WORD.findall(sentence)) if w not in _STOP}


def numbers(sentence):
    return {n.replace(",", "").rstrip(".") for n in _NUMBER.findall(sentence)}


# ---------------------------------------------------------------- families and independence

def load_families(path=None):
    p = pathlib.Path(path) if path else HERE / "families.json"
    return json.loads(p.read_text(encoding="utf-8"))


def family_of(source, fam, meta_sources=None):
    fam_id = fam.get("sources", {}).get(source)
    if not fam_id and meta_sources:
        fam_id = (meta_sources.get(source) or {}).get("family")
    return fam_id or source


def cluster_of(family, fam):
    return (fam.get("families", {}).get(family) or {}).get("cluster", family)


def rho(a, b, fam, meta_sources=None):
    if a == b:
        return 1.0
    fa, fb = family_of(a, fam, meta_sources), family_of(b, fam, meta_sources)
    r = fam["rho"]
    if fa == fb:
        return r["same_family"]
    if cluster_of(fa, fam) == cluster_of(fb, fam):
        return r["same_cluster"]
    return r["other"]


def voices(sources, fam, meta_sources=None):
    """Independent voices = N^2 / sum_ij rho_ij (rho_ii = 1); N/(1+(N-1)rho) for uniform rho."""
    s = sorted(set(sources))
    if not s:
        return 0.0
    total = sum(rho(a, b, fam, meta_sources) for a in s for b in s)
    return len(s) ** 2 / total


def rho_note(fam):
    r = fam["rho"]
    return (f"assumed correlations: same family {r['same_family']}, same cluster {r['same_cluster']}, "
            f"otherwise {r['other']}")


# ---------------------------------------------------------------- loading a run

class RunError(Exception):
    pass


def load_run(run, claims_path=None, map_path=None):
    run = pathlib.Path(run)
    answers_dir = run / "answers"
    meta_path = run / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    sources = dict(meta.get("sources") or {})
    if answers_dir.is_dir():
        for f in sorted(answers_dir.glob("*.md")):
            if f.stem not in sources:
                sources[f.stem] = {"status": "ok" if f.read_text(encoding="utf-8").strip() else "empty"}
    texts = {}
    for sid, info in sources.items():
        f = answers_dir / f"{sid}.md"
        texts[sid] = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
        if info.get("status") in USABLE and not texts[sid].strip():
            info["status"] = "empty"
    cpath = pathlib.Path(claims_path) if claims_path else run / "claims.json"
    claims_doc = {}
    if cpath.exists():
        try:
            claims_doc = json.loads(cpath.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise RunError(f"{cpath.name} is not valid JSON: {e}")
    if isinstance(claims_doc, list):
        claims_doc = {"claims": claims_doc}
    mapping = {}
    mp = pathlib.Path(map_path) if map_path else None
    if mp is None and (run / "blind" / "map.json").exists() and _uses_labels(claims_doc, run):
        mp = run / "blind" / "map.json"
    if mp is not None:
        mapping = json.loads(mp.read_text(encoding="utf-8"))
    return {"run": run, "meta": meta, "sources": sources, "texts": texts,
            "norm": {k: norm(v) for k, v in texts.items()}, "claims": claims_doc, "map": mapping}


def _uses_labels(doc, run):
    labels = {p.stem for p in (run / "blind").glob("*.md")}
    for c in doc.get("claims", []) if isinstance(doc, dict) else []:
        for e in (c.get("support") or []) + (c.get("contradict") or []):
            if isinstance(e, dict) and e.get("source") in labels:
                return True
    return False


def usable(R):
    return [s for s, i in R["sources"].items() if i.get("status") in USABLE]


# ---------------------------------------------------------------- validation

def check(R):
    """Prove every quote. Returns (errors, clean_claims, unverified)."""
    errors, clean, unverified = [], [], []
    ok_sources = set(usable(R))
    mapping = R["map"]
    raw = R["claims"].get("claims")
    if raw is None:
        return ["claims.json has no 'claims' list (write it before validate)"], [], []
    seen = set()
    for n, c in enumerate(raw, 1):
        if not isinstance(c, dict):
            errors.append(f"claim #{n}: not an object")
            continue
        cid = str(c.get("id") or f"c{n}")
        if cid in seen:
            errors.append(f"{cid}: duplicate id")
        seen.add(cid)
        text = str(c.get("text") or "").strip()
        if not text:
            errors.append(f"{cid}: empty text")
            continue
        kind = c.get("kind", "fact")
        if kind not in KINDS:
            errors.append(f"{cid}: kind must be one of {', '.join(KINDS)}")
            kind = "fact"
        item = {"id": cid, "text": text, "kind": kind, "support": [], "contradict": []}
        for side in ("support", "contradict"):
            for e in c.get(side) or []:
                if not isinstance(e, dict):
                    errors.append(f"{cid}: {side} entry is not an object")
                    continue
                src = mapping.get(e.get("source"), e.get("source"))
                quote = str(e.get("quote") or "")
                entry = {"source": src, "quote": quote}
                if side == "contradict":
                    entry["says"] = str(e.get("says") or "").strip()
                why = ""
                if src not in R["sources"]:
                    why = f"unknown source '{src}'"
                elif src not in ok_sources:
                    why = f"source '{src}' has no usable answer (status {R['sources'][src].get('status')})"
                else:
                    found, why = find_quote(quote, R["norm"][src])
                    if found:
                        why = ""
                if why:
                    errors.append(f"{cid}: {side} from {src}: {why}")
                    unverified.append({"id": cid, "text": text, "side": side, "source": src,
                                       "quote": quote, "why": why})
                else:
                    item[side].append(entry)
        if not item["support"]:
            if not any(u["id"] == cid and u["side"] == "support" for u in unverified):
                errors.append(f"{cid}: no supporting quote (every claim needs at least one)")
                unverified.append({"id": cid, "text": text, "side": "support", "source": "",
                                   "quote": "", "why": "no supporting quote"})
            continue
        dup = {e["source"] for e in item["support"]} & {e["source"] for e in item["contradict"]}
        if dup:
            errors.append(f"{cid}: {', '.join(sorted(dup))} both supports and contradicts; pick one side")
        clean.append(item)
    return errors, clean, unverified


def possibly_missed(R):
    """Sentences with a number, date or name that no quote (or skip note) of that source covers."""
    covered = {}
    for c in R["claims"].get("claims") or []:
        if not isinstance(c, dict):
            continue
        for e in (c.get("support") or []) + (c.get("contradict") or []):
            if isinstance(e, dict):
                src = R["map"].get(e.get("source"), e.get("source"))
                covered.setdefault(src, []).extend(quote_fragments(str(e.get("quote") or "")))
    for e in R["claims"].get("skipped") or []:
        if isinstance(e, dict):
            src = R["map"].get(e.get("source"), e.get("source"))
            covered.setdefault(src, []).extend(quote_fragments(str(e.get("quote") or "")))
    out = {}
    for src in usable(R):
        frags = [f for f in covered.get(src, []) if len(f) >= 6]
        cover_words = set()
        for f in frags:
            cover_words |= keywords(f)
        missed = []
        for s in sentences(R["texts"][src]):
            if not (_NUMBER.search(s) or _PROPER.search(s)):
                continue
            ns = norm(s)
            if any(f in ns or ns in f for f in frags):
                continue
            kw = keywords(s)
            if kw and len(kw & cover_words) / len(kw) >= 0.6:
                continue
            missed.append(s[:300])
        if missed:
            out[src] = missed[:MAX_MISSED_PER_SOURCE] + (
                [f"... and {len(missed) - MAX_MISSED_PER_SOURCE} more"] if len(missed) > MAX_MISSED_PER_SOURCE else [])
    return out


def number_mismatches(R):
    """Mechanical scan: sentences from different sources sharing 2+ keywords but citing different numbers."""
    rows = []
    for src in usable(R):
        for s in sentences(R["texts"][src]):
            nums = numbers(s)
            if nums:
                rows.append((src, s, keywords(s), nums))
    out, seen = [], set()
    for i, (sa, ta, ka, na) in enumerate(rows):
        for sb, tb, kb, nb in rows[i + 1:]:
            if sa == sb or na == nb or na & nb:
                continue
            shared = ka & kb
            if len(shared) >= 2:
                key = (sa, sb, tuple(sorted(shared))[:3])
                if key in seen:
                    continue
                seen.add(key)
                out.append({"a": sa, "a_numbers": sorted(na), "b": sb, "b_numbers": sorted(nb),
                            "shared_words": sorted(shared)[:6], "a_text": ta[:200], "b_text": tb[:200]})
                if len(out) >= MAX_NUMBER_PAIRS:
                    return out
    return out


def injection_suspects(R):
    out = {}
    for src, text in R["texts"].items():
        hits = []
        for line in (text or "").splitlines():
            if any(p.search(line) for p in _INJECTION):
                hits.append(line.strip()[:200])
        if hits:
            out[src] = hits[:5]
    return out


def validate(R):
    errors, clean, unverified = check(R)
    return {
        "ok": not errors,
        "errors": errors,
        "claims_ok": len(clean),
        "usable_sources": usable(R),
        "possibly_missed": possibly_missed(R) if R["claims"].get("claims") is not None else {},
        "number_mismatches": number_mismatches(R),
        "injection_suspect": injection_suspects(R),
    }


# ---------------------------------------------------------------- classification

def classify(clean, R, fam):
    ms = R["sources"]
    groups = {"agreed": [], "disputed": [], "cluster": [], "echo": [], "single": []}
    for c in clean:
        sup = sorted({e["source"] for e in c["support"]})
        c["voices"] = voices(sup, fam, ms)
        if c["contradict"]:
            con = sorted({e["source"] for e in c["contradict"]})
            c["con_voices"] = voices(con, fam, ms)
            groups["disputed"].append(c)
        elif len(sup) == 1:
            groups["single"].append(c)
        elif len({cluster_of(family_of(s, fam, ms), fam) for s in sup}) >= 2:
            groups["agreed"].append(c)
        elif len({family_of(s, fam, ms) for s in sup}) >= 2:
            groups["cluster"].append(c)  # different families that share a cluster: partly independent
        else:
            groups["echo"].append(c)
    return groups


def minority(groups, clean, R):
    """Smaller side of each dispute, shown whole; plus the most isolated source's core paragraph."""
    passages = []
    for c in groups["disputed"]:
        if c["con_voices"] < c["voices"]:
            side, entries = "contradicts", c["contradict"]
        elif c["voices"] < c["con_voices"]:
            side, entries = "supports", c["support"]
        else:
            continue
        for e in entries:
            para = paragraph_with(R["texts"][e["source"]], e["quote"]) or e["quote"]
            passages.append({"id": c["id"], "text": c["text"], "side": side, "source": e["source"],
                             "passage": para[:1500]})
    counts = {}
    for c in clean:
        lonely = c in groups["disputed"] or c in groups["single"]
        for s in {e["source"] for e in c["support"] + c["contradict"]}:
            t = counts.setdefault(s, [0, 0])
            t[0] += 1
            t[1] += 1 if lonely else 0
    outlier = None
    best = 0.0
    for s, (total, lonely) in sorted(counts.items()):
        ratio = lonely / total if total else 0
        if total >= 2 and ratio >= 0.5 and ratio > best:
            best, outlier = ratio, s
    core = ""
    if outlier:
        for para in re.split(r"\n\s*\n", R["texts"][outlier]):
            if len(para.split()) >= 12:
                core = para.strip()[:1500]
                break
    return passages, (outlier, best, core)


# ---------------------------------------------------------------- rendering

def _src_list(entries):
    return ", ".join(sorted({e["source"] for e in entries}))


def _quote_lines(entries, limit=240):
    lines = []
    for e in entries:
        q = _SPACE.sub(" ", e["quote"]).strip()
        if len(q) > limit:
            q = q[:limit].rstrip() + " ..."
        lines.append(f"  > \"{q}\" ({e['source']})")
    return lines


def _fmt_time(info):
    t = info.get("elapsed_s")
    return f"{float(t):.0f} s" if isinstance(t, (int, float)) else ""


def render(R, fam):
    meta, srcs = R["meta"], R["sources"]
    question = (meta.get("question") or R["claims"].get("question") or "").strip()
    title = meta.get("title") or "Report"
    use = usable(R)
    lines = [f"# {title}", ""]
    if question:
        q = question if len(question) <= 600 else question[:600].rstrip() + " ..."
        lines += ["**Question:** " + q.replace("\n", " "), ""]
    if meta.get("run_line"):
        lines += [meta["run_line"], ""]
    single = len(use) < 2
    if single:
        lines += ["> **This is one opinion, not a comparison.** Fewer than two sources gave a usable answer, "
                  "so nothing below counts as agreement.", ""]

    errors, clean, unverified = check(R) if R["claims"].get("claims") is not None else ([], [], [])
    bottom = R["claims"].get("bottom_line") or []
    lines += ["## Bottom line", ""]
    if bottom:
        for b in bottom[:5]:
            if isinstance(b, dict):
                tag = ", ".join(b.get("sources") or [])
                lines.append(f"- {str(b.get('text', '')).strip()} [{tag}]")
            else:
                lines.append(f"- {str(b).strip()}")
    else:
        lines.append(PLACEHOLDER_BOTTOM)
    lines.append("")

    lines += ["## Who answered", "", "| source | family | mode | time | status |", "|---|---|---|---|---|"]
    inj = injection_suspects(R)
    for sid in sorted(srcs):
        info = srcs[sid]
        status = info.get("status", "?")
        flags = list(info.get("flags") or [])
        if sid in inj:
            flags.append("injection-suspect")
        if info.get("note"):
            flags.append(str(info["note"]))
        shown = status + (f" ({'; '.join(flags)})" if flags else "")
        mode = info.get("model") or info.get("mode") or ""
        if info.get("model") and info.get("mode"):
            mode = f"{info['mode']}, {info['model']}"
        lines.append(f"| {sid} | {family_of(sid, fam, srcs)} | {mode} | {_fmt_time(info)} | {shown} |")
    lines.append("")

    if not single and clean:
        groups = classify(clean, R, fam)
        if groups["agreed"]:
            lines += ["## Agreed across families", ""]
            for c in groups["agreed"]:
                lines.append(f"- **{c['id']}** {c['text']} [{_src_list(c['support'])}] "
                             f"(about {c['voices']:.1f} independent voices)")
                lines += _quote_lines(c["support"])
            lines.append("")
        if groups["disputed"]:
            lines += ["## Disputed", "", "Both sides are shown; the script does not pick a winner.", ""]
            for c in groups["disputed"]:
                lines.append(f"- **{c['id']}** {c['text']}")
                lines.append(f"  - For it: {_src_list(c['support'])} (about {c['voices']:.1f} independent voices)")
                lines += ["  " + x for x in _quote_lines(c["support"])]
                says = "; ".join(sorted({e["says"] for e in c["contradict"] if e.get("says")}))
                lines.append(f"  - Against it: {_src_list(c['contradict'])}"
                             + (f", saying: {says}" if says else "")
                             + f" (about {c['con_voices']:.1f} independent voices)")
                lines += ["  " + x for x in _quote_lines(c["contradict"])]
            lines.append("")
        if groups["cluster"]:
            lines += [f"## Agreement within one cluster (assumed correlation {fam['rho']['same_cluster']})", "",
                      "Different model families that share a cluster: partly independent, weaker than agreement "
                      "across clusters.", ""]
            for c in groups["cluster"]:
                lines.append(f"- **{c['id']}** {c['text']} [{_src_list(c['support'])}] "
                             f"(about {c['voices']:.1f} independent voices)")
                lines += _quote_lines(c["support"])
            lines.append("")
        if groups["echo"]:
            lines += ["## Echo (repeated inside one family: treat as one voice)", ""]
            for c in groups["echo"]:
                lines.append(f"- **{c['id']}** {c['text']} [{_src_list(c['support'])}]")
                lines += _quote_lines(c["support"])
            lines.append("")
        if groups["single"]:
            lines += ["## Single-source (a unique find or one model's error: check it)", ""]
            for c in groups["single"]:
                lines.append(f"- **{c['id']}** {c['text']} [{_src_list(c['support'])}]")
                lines += _quote_lines(c["support"])
            lines.append("")
        passages, (outlier, ratio, core) = minority(groups, clean, R)
        if passages or core:
            lines += ["## Minority report", ""]
            for p in passages:
                lines.append(f"- {p['source']} {p['side']} **{p['id']}** ({p['text']}), in its own words:")
                lines += ["", *("  > " + x for x in p["passage"].splitlines()), ""]
            if core:
                lines.append(f"- {outlier} stands apart most often ({ratio:.0%} of its claims are disputed or "
                             f"made by it alone). The core of its answer ([answers/{outlier}.md](answers/{outlier}.md)):")
                lines += ["", *("  > " + x for x in core.splitlines()), ""]
            lines.append("")

    if unverified:
        lines += ["## Could not be verified", "",
                  "These entries quoted text that is not in the saved answer, so they were not counted.", ""]
        for u in unverified:
            who = f" ({u['source']}, {u['side']})" if u["source"] else ""
            lines.append(f"- **{u['id']}** {u['text']}{who}: {u['why']}")
        lines.append("")

    n = len(use)
    lines += ["## How independent are these answers?", ""]
    if n:
        lines.append(f"{n} usable answer{'s' if n != 1 else ''} = about {voices(use, fam, srcs):.1f} "
                     f"independent voices under {rho_note(fam)}.")
    else:
        lines.append("No usable answers.")
    lines += ["These correlations are assumptions, not measurements. To measure your own models' correlation, "
              "use the teaching calculator in https://github.com/vadimchernets/after-chat.", ""]

    checks = []
    missed = possibly_missed(R) if R["claims"].get("claims") is not None else {}
    total_missed = sum(len([x for x in v if not x.startswith("... and")]) for v in missed.values())
    if total_missed:
        checks.append(f"- {total_missed} sentence(s) with a number, date or name are not covered by any claim "
                      f"(run `validate` to list them).")
    mism = number_mismatches(R)
    if mism:
        checks.append(f"- {len(mism)} place(s) where sources cite different numbers near the same words "
                      f"(mechanical scan, may be noise). First: {mism[0]['a']} says "
                      f"{', '.join(mism[0]['a_numbers'])}, {mism[0]['b']} says {', '.join(mism[0]['b_numbers'])}.")
    for sid, hits in sorted(inj.items()):
        checks.append(f"- {sid}'s answer contains text addressed to an AI agent (\"{hits[0][:80]}\"). "
                      f"It was treated as data and not followed.")
    if checks:
        lines += ["## Checks", "", *checks, ""]

    lines += ["## Claude's own view", ""]
    own = str(R["claims"].get("own_view") or "").strip()
    own_file = R["run"] / "own.md"
    drafted = own_file.read_text(encoding="utf-8").strip() if own_file.exists() else ""
    if drafted:
        lines += ["_Drafted before reading the others; not counted in any section above._", "", drafted, ""]
    if own:
        lines += ["_Written after reading the others; not counted in any section above._", "", own]
    if not drafted and not own:
        lines.append(PLACEHOLDER_OWN)
    lines.append("")

    lines += ["## Files", ""]
    for sid in sorted(srcs):
        if (R["run"] / "answers" / f"{sid}.md").exists():
            lines.append(f"- [answers/{sid}.md](answers/{sid}.md)")
    for extra in ("claims.json", "meta.json", "own.md"):
        if (R["run"] / extra).exists():
            lines.append(f"- [{extra}]({extra})")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------- lint

def lint(R, report_text):
    """Every Bottom-line bullet ends with [source, ...] naming sources that answered."""
    problems = []
    ok = set(usable(R))
    m = re.search(r"^## Bottom line\s*$(.*?)(?=^## |\Z)", report_text, re.M | re.S)
    if not m:
        return ["no '## Bottom line' section"]
    body = m.group(1)
    if PLACEHOLDER_BOTTOM in body:
        problems.append("Bottom line not written yet (placeholder still there)")
    bullets = [b.strip() for b in re.findall(r"^\s*[-*]\s+(.*)$", body, re.M)]
    if not bullets and PLACEHOLDER_BOTTOM not in body:
        problems.append("Bottom line has no bullets")
    if len(bullets) > 5:
        problems.append(f"Bottom line has {len(bullets)} bullets (at most 5)")
    for b in bullets:
        t = re.search(r"\[([^\[\]]+)\]\s*\.?$", b)
        if not t:
            problems.append(f"untagged Bottom-line bullet: {b[:80]}")
            continue
        for s in (x.strip() for x in t.group(1).split(",")):
            if s not in ok:
                problems.append(f"Bottom-line tag names '{s}', which gave no usable answer: {b[:60]}")
    if not re.search(r"^## Claude's own view\s*$", report_text, re.M):
        problems.append("no '## Claude's own view' section")
    return problems


# ---------------------------------------------------------------- command line

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("validate", "render"):
        p = sub.add_parser(name)
        p.add_argument("run")
        p.add_argument("--claims")
        p.add_argument("--map")
        p.add_argument("--families")
        if name == "render":
            p.add_argument("--out")
    p = sub.add_parser("lint")
    p.add_argument("run")
    p.add_argument("--report")
    p = sub.add_parser("voices")
    p.add_argument("sources", nargs="+")
    p.add_argument("--families")
    a = ap.parse_args(argv)
    try:
        if a.cmd == "voices":
            fam = load_families(a.families)
            print(json.dumps({"sources": a.sources, "independent_voices": round(voices(a.sources, fam), 2),
                              "assumed": True, "note": rho_note(fam)}))
            return 0
        if not pathlib.Path(a.run).is_dir():
            raise RunError(f"run folder not found: {a.run}")
        if a.cmd == "lint":
            R = load_run(a.run)
            rp = pathlib.Path(a.report) if a.report else R["run"] / "report.md"
            if not rp.exists():
                raise RunError(f"report not found: {rp}")
            problems = lint(R, rp.read_text(encoding="utf-8"))
            print(json.dumps({"ok": not problems, "problems": problems}, indent=2))
            return 0 if not problems else 1
        R = load_run(a.run, a.claims, a.map)
        fam = load_families(a.families)
        if a.cmd == "validate":
            res = validate(R)
            (R["run"] / "validation.json").write_text(json.dumps(res, indent=2), encoding="utf-8")
            print(json.dumps(res, indent=2))
            return 0 if res["ok"] else 1
        text = render(R, fam)
        out = pathlib.Path(a.out) if a.out else R["run"] / "report.md"
        out.write_text(text, encoding="utf-8")
        print(str(out))
        return 0
    except (RunError, json.JSONDecodeError, OSError, KeyError) as e:
        print(f"merge.py: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
