#!/usr/bin/env python3
"""In-capsule Python AST allowlist checker (FX3 ruling, AW1-05).

stdin: script text. stdout: one JSON verdict. rc 0 allow, 5 ast_denied, 2 usage.
Closed allowlist: everything not named in allowlist.json is denied (fail closed).
Stdlib only; never exits 1.
"""
import ast
import io
import json
import os
import posixpath
import sys
import tokenize

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = "os.cu.ast/v1"


class Denied(Exception):
    pass


def deny(reason):
    raise Denied(reason)


def in_out(path, out_dir):
    return (bool(out_dir) and posixpath.isabs(path) and posixpath.normpath(path) == path
            and (path == out_dir or path.startswith(out_dir.rstrip("/") + "/")))


def check_tokens(text):
    """Reject non-ASCII outside plain string tokens/comments before ast.parse can NFKC-fold it."""
    fstart = getattr(tokenize, "FSTRING_START", None)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == fstart and fstart is not None:
                deny("f-string")
            if tok.type == tokenize.COMMENT:
                continue
            if tok.type == tokenize.STRING:
                body = tok.string
                q = min([i for i in (body.find("'"), body.find('"')) if i >= 0])
                if not body[:q].isascii():
                    deny("non-ascii string prefix")
                if "f" in body[:q].lower():
                    deny("f-string")
                continue
            if not tok.string.isascii():
                deny("non-ascii outside string literal")
    except (tokenize.TokenError, SyntaxError, IndentationError) as e:
        deny("syntax: " + type(e).__name__)


def chain(node):
    """Flatten a pure Name.attr.attr chain into (root, [attrs]); (None, []) otherwise."""
    attrs = []
    while isinstance(node, ast.Attribute):
        attrs.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        return node.id, attrs[::-1]
    return None, []


def base_of(node):
    """Leftmost base of an Attribute/Subscript chain; a Call result is never a chain root (ADW2-01)."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node


def target_names_only(tg):
    """Unpacking/loop targets may only be Names (possibly nested in Tuple/List)."""
    if isinstance(tg, (ast.Tuple, ast.List)):
        return all(target_names_only(e) for e in tg.elts)
    return isinstance(tg, ast.Name)


class Checker:
    def __init__(self, allow, out_dir):
        self.a = allow
        self.out = out_dir
        self.nodes = set(allow["nodes"])
        self.mods = set(allow["modules"])
        self.builtins = set(allow["builtins"])
        self.stores = set()
        self.imported = set()
        self.attr_values = set()  # non-maximal Attribute nodes
        self.name_roots = set()   # Name nodes that are the root of an Attribute
        self.call_funcs = set()   # id(Call.func): the only place a callable attribute may appear (ADW3-01)
        self.count = 0

    def prepass(self, tree):
        for n in ast.walk(tree):
            self.count += 1
            if self.count > self.a["max_nodes"]:
                deny("too many nodes")
            if type(n).__name__ not in self.nodes:
                deny("node " + type(n).__name__)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                self.stores.add(n.id)
            if isinstance(n, ast.Call):
                self.call_funcs.add(id(n.func))
            if isinstance(n, ast.Attribute):
                if isinstance(n.value, ast.Attribute):
                    self.attr_values.add(id(n.value))
                if isinstance(n.value, ast.Name):
                    self.name_roots.add(id(n.value))
            if isinstance(n, ast.ImportFrom):
                for al in n.names:
                    self.imported.add(al.name)

    def lit_path(self, v, what):
        if not (isinstance(v, ast.Constant) and isinstance(v.value, str) and in_out(v.value, self.out)):
            deny(what + " must be a literal path under out-dir")

    def chain_ok(self, root, attrs):
        parts = [root] + attrs
        dotted = ".".join(parts)
        for p in self.a["chain_any"]:
            if dotted.startswith(p + ".") or (dotted == p and p not in self.a["chain_min2"]):
                return
        for p in self.a["chain_deeper"]:
            if dotted.startswith(p + "."):
                if p == "bpy.ops.wm":
                    op = parts[3]
                    for d in self.a["wm_deny_prefix"]:
                        if op.startswith(d):
                            deny("bpy.ops.wm." + op)
                return
        deny("chain " + dotted)

    def stored_pass(self, tree):
        """ADW3-01: a path/ops callable may only appear as Call.func; run after visit so older reasons keep priority."""
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and id(n) not in self.call_funcs:
                if n.attr in self.a["path_calls"]:
                    deny("stored_callable " + n.attr)
                root, attrs = chain(n)
                if root == "bpy" and attrs[:1] == ["ops"] and id(n) not in self.attr_values:
                    deny("stored_callable bpy.ops")

    def visit(self, n):
        t = type(n).__name__
        if t == "Constant":
            v = n.value
            if isinstance(v, bytes):
                deny("bytes literal")
            if isinstance(v, str):
                if ".." in v:
                    deny("dotdot in string")
                if v.startswith("/") and not in_out(v, self.out):
                    deny("path literal outside out-dir")
                if v in self.a["str_deny"]:
                    deny("string " + v)
        elif t == "Name":
            if n.id.startswith("_"):
                deny("underscore name")
            if isinstance(n.ctx, ast.Store):
                if n.id in self.mods:
                    deny("module name rebound")
            elif n.id in self.mods:
                if id(n) not in self.name_roots:
                    deny("bare module " + n.id)
            elif not (n.id in self.builtins or n.id in self.stores or n.id in self.imported):
                deny("name " + n.id)
        elif t == "Attribute":
            if n.attr.startswith("_"):
                deny("underscore attribute")
            if n.attr in self.a["attr_deny"]:
                deny("attribute " + n.attr)
            if id(n) not in self.attr_values:
                if not isinstance(base_of(n), ast.Name):
                    deny("non-name chain root")
                root, attrs = chain(n)
                if root in self.mods:
                    self.chain_ok(root, attrs)
        elif t == "Call":
            f = n.func
            if isinstance(f, ast.Name):
                if not (f.id in self.builtins or f.id in self.imported):
                    deny("call " + f.id)
            elif not isinstance(f, ast.Attribute):
                deny("indirect call")
            if n.args and isinstance(f, ast.Attribute):
                root, attrs = chain(f)
                if root == "bpy" and attrs[:1] == ["ops"]:
                    deny("bpy.ops positional args")
            for kw in n.keywords:
                if kw.arg is None:
                    deny("**kwargs")
                if kw.arg.startswith("_") or kw.arg in self.a["kw_deny"]:
                    deny("keyword " + kw.arg)
                if kw.arg in self.a["path_kwargs"]:
                    self.lit_path(kw.value, kw.arg)
            if isinstance(f, ast.Attribute) and f.attr in self.a["path_calls"]:
                if not isinstance(base_of(f), ast.Name):
                    deny("non-name chain root")
                if not (n.args or any(kw.arg in self.a["path_kwargs"] for kw in n.keywords)):
                    deny("path_call_without_path " + f.attr)
                if n.args:
                    self.lit_path(n.args[0], f.attr)
        elif t in ("Assign", "AugAssign"):
            targets = n.targets if t == "Assign" else [n.target]
            for tg in targets:
                if not (isinstance(tg, ast.Attribute) or target_names_only(tg)):
                    deny("non-name target")
                if isinstance(tg, ast.Attribute):
                    b = base_of(tg)
                    if isinstance(b, ast.Name) and b.id in self.mods and b.id != "bpy":
                        deny("stored_callable module attribute assignment")
                if isinstance(tg, ast.Attribute) and tg.attr in self.a["path_kwargs"]:
                    if t == "AugAssign":
                        deny("augmented path assignment")
                    self.lit_path(n.value, tg.attr)
        elif t == "For":
            if not target_names_only(n.target):
                deny("non-name target")
        elif t == "comprehension":
            if not target_names_only(n.target):
                deny("non-name target")
        elif t == "Import":
            for al in n.names:
                if al.name not in self.mods or al.asname:
                    deny("import " + al.name)
        elif t == "ImportFrom":
            ok = self.a["from_import"].get(n.module or "")
            if n.level or ok is None:
                deny("from " + str(n.module))
            for al in n.names:
                if al.asname or al.name.startswith("_") or (al.name not in ok and "*" not in ok) or al.name == "*":
                    deny("from-import " + al.name)
        for c in ast.iter_child_nodes(n):
            self.visit(c)


def verdict(text, out_dir, allow):
    if len(text.encode("utf-8", "surrogatepass")) > allow["max_bytes"]:
        deny("too large")
    check_tokens(text)
    try:
        tree = ast.parse(text, mode="exec")
    except (SyntaxError, ValueError, RecursionError, MemoryError) as e:
        deny("syntax: " + type(e).__name__)
    c = Checker(allow, out_dir)
    c.prepass(tree)
    c.visit(tree)
    c.stored_pass(tree)


def main(argv):
    out_dir, i = "", 0
    while i < len(argv):
        a = argv[i]
        if a == "--out-dir" and i + 1 < len(argv):
            out_dir, i = argv[i + 1], i + 2
        elif a.startswith("--out-dir="):
            out_dir, i = a[len("--out-dir="):], i + 1
        else:
            sys.stderr.write("usage: astcheck.py [--out-dir D] < script\n")
            return 2
    res = {"schema": SCHEMA, "verdict": "allow", "code": "", "reason": ""}
    rc = 0
    try:
        with open(os.path.join(HERE, "allowlist.json"), encoding="utf-8") as fh:
            allow = json.load(fh)
        raw = sys.stdin.buffer.read(allow["max_bytes"] + 1)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            deny("invalid utf-8")
        verdict(text, out_dir, allow)
    except Denied as e:
        res.update(verdict="deny", code="ast_denied", reason=str(e))
        rc = 5
    except Exception as e:  # fail closed, never rc 1
        res.update(verdict="deny", code="ast_denied", reason="internal: " + type(e).__name__)
        rc = 5
    sys.stdout.write(json.dumps(res, sort_keys=True) + "\n")
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
