#!/usr/bin/env python3
"""
Stamp the engine's content hash into the pages that import it.

index.html and engine.js are two files, cached independently. GitHub Pages
serves both with max-age=600, so for ten minutes after a deploy a returning
visitor can hold a fresh index.html and a stale engine.js. If the new page
imports something the old engine does not export, the module never parses and
the whole page is dead:

    Uncaught SyntaxError: The requested module './engine.js'
    does not provide an export named 'schedule'

which is exactly what shipping the schedule() export did. Pinning the import to
a hash of the engine's own bytes closes it: a page can only ever ask for the
engine it was built against, and that URL was never in anyone's cache.

Use:
    python3 stamp_engine.py            # rewrite the imports to match
    python3 stamp_engine.py --check    # exit 1 if they are stale (pre-commit)
    python3 stamp_engine.py --selftest
"""

import argparse
import hashlib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ENGINE = ROOT / "docs" / "engine.js"
PAGES = [ROOT / "docs" / "index.html", ROOT / "docs" / "basketball" / "index.html"]

# the import specifier, with an optional ?v= already on it
IMPORT_RE = re.compile(r"""(from\s+['"])((?:\.\./|\./)engine\.js)(\?v=[0-9a-f]+)?(['"])""")


def token(engine_bytes):
    """Twelve hex digits of SHA-256 -- short enough to read in a diff, long
    enough that a collision is not a thing to worry about."""
    return hashlib.sha256(engine_bytes).hexdigest()[:12]


def restamp(text, tok):
    """Returns (new_text, number_of_imports_found)."""
    n = 0

    def sub(m):
        nonlocal n
        n += 1
        return f"{m.group(1)}{m.group(2)}?v={tok}{m.group(4)}"

    return IMPORT_RE.sub(sub, text), n


def run(check):
    tok = token(ENGINE.read_bytes())
    stale = []
    for p in PAGES:
        text = p.read_text(encoding="utf-8")
        new, n = restamp(text, tok)
        if n == 0:
            print(f"ERROR: no engine.js import found in {p}", file=sys.stderr)
            return 2
        if new == text:
            print(f"  ok     {p.relative_to(ROOT)}  (?v={tok})")
            continue
        stale.append(p)
        if check:
            print(f"  STALE  {p.relative_to(ROOT)}  (should be ?v={tok})")
        else:
            p.write_text(new, encoding="utf-8")
            print(f"  wrote  {p.relative_to(ROOT)}  (?v={tok})")
    if check and stale:
        print("\nengine.js changed without restamping. Run: python3 stamp_engine.py",
              file=sys.stderr)
        return 1
    return 0


def selftest():
    checks = 0

    def eq(got, want, label):
        nonlocal checks
        checks += 1
        assert got == want, f"{label}: got {got!r}, wanted {want!r}"

    # token is stable, short, and moves when the bytes move
    eq(len(token(b"abc")), 12, "token length")
    eq(token(b"abc"), token(b"abc"), "token is stable")
    assert token(b"abc") != token(b"abd"), "token must move with the bytes"
    checks += 1

    # a bare import gets stamped
    got, n = restamp("import {a} from './engine.js';", "deadbeef1234")
    eq(got, "import {a} from './engine.js?v=deadbeef1234';", "stamp bare")
    eq(n, 1, "count bare")

    # an already-stamped import is replaced, not doubled
    got, n = restamp("from './engine.js?v=0000aaaa1111'", "deadbeef1234")
    eq(got, "from './engine.js?v=deadbeef1234'", "restamp existing")
    eq(n, 1, "count existing")

    # the basketball page's relative path
    got, n = restamp('from "../engine.js"', "deadbeef1234")
    eq(got, 'from "../engine.js?v=deadbeef1234"', "stamp parent path")
    eq(n, 1, "count parent")

    # multi-line import, which is how both pages actually write it
    src = "import {WIN, esc,\n        schedule} from './engine.js';"
    got, n = restamp(src, "deadbeef1234")
    eq(got, "import {WIN, esc,\n        schedule} from './engine.js?v=deadbeef1234';",
       "stamp multiline")
    eq(n, 1, "count multiline")

    # leaves other modules alone
    got, n = restamp("from './other.js'; from './engine.js';", "deadbeef1234")
    eq(got, "from './other.js'; from './engine.js?v=deadbeef1234';", "other modules untouched")
    eq(n, 1, "count other")

    # idempotent: stamping twice changes nothing the second time
    once, _ = restamp("from './engine.js';", "deadbeef1234")
    twice, _ = restamp(once, "deadbeef1234")
    eq(twice, once, "idempotent")

    # the real files each hold exactly one import
    for p in PAGES:
        _, n = restamp(p.read_text(encoding="utf-8"), "deadbeef1234")
        eq(n, 1, f"exactly one import in {p.name}")

    print(f"all checks passed ({checks})")
    return 0


def main():
    ap = argparse.ArgumentParser(description="Stamp the engine hash into its importers")
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if a page is stamped with the wrong hash")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    return run(a.check)


if __name__ == "__main__":
    sys.exit(main())
