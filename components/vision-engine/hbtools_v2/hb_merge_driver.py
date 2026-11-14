#!/usr/bin/env python3
"""HB2-09: git merge drivers. `hb_merge_driver.py regen %O %A %B %P` (derived files: keep ours, regenerate after the merge) and
`hb_merge_driver.py book %O %A %B %P` (book.md: line merge outside generated fences; any difference inside hb-s8..hb-s10 against the
base on either side is E_PIN, rc 1). A prose conflict is rc 1 (git leaves the file conflicted)."""
import re, subprocess, sys, os, tempfile

sys.dont_write_bytecode = True
GEN = re.compile(r"(<!-- HB:GEN BEGIN (hb-s\d+) -->\n)(.*?)(<!-- HB:GEN END \2 -->\n)", re.S)


def fences(text):
    return {m.group(2): m.group(3) for m in GEN.finditer(text)}


def mask(text):
    return GEN.sub(lambda m: m.group(1) + "@@GEN %s@@\n" % m.group(2) + m.group(4), text)


def merge_book(base, ours, theirs):
    """-> (rc, merged text or None, code)."""
    fb, fo, ft = fences(base), fences(ours), fences(theirs)
    if fo != fb or ft != fb:
        return 1, None, "E_PIN"
    d = tempfile.mkdtemp(prefix="hbmerge")
    paths = {}
    for k, t in (("o", mask(ours)), ("b", mask(base)), ("t", mask(theirs))):
        paths[k] = os.path.join(d, k)
        open(paths[k], "w", encoding="utf-8", newline="").write(t)
    p = subprocess.run(["git", "merge-file", "-p", paths["o"], paths["b"], paths["t"]], capture_output=True, text=True)
    if p.returncode != 0:
        return 1, None, "CONFLICT"
    merged = p.stdout
    merged = re.sub(r"(<!-- HB:GEN BEGIN (hb-s\d+) -->\n)@@GEN \2@@\n", lambda m: m.group(1) + fb[m.group(2)], merged)
    return 0, merged, "OK"


def main(argv=None):
    a = sys.argv[1:] if argv is None else argv
    if len(a) < 4:
        return 2
    mode, o, cur, b = a[0], a[1], a[2], a[3]
    if mode == "regen":
        return 0                                    # keep ours; one `hb_build --render-derived` follows the merge
    if mode == "book":
        rd = lambda p: open(p, encoding="utf-8", newline="").read()
        rc, merged, code = merge_book(rd(o), rd(cur), rd(b))
        if rc != 0:
            sys.stderr.write(code + "\n")
            return 1
        open(cur, "w", encoding="utf-8", newline="").write(merged)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
