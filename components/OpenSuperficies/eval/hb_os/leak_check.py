#!/usr/bin/env python3
"""Leakage and blind-split guard for OpenSuperficies eval/hb_os query sets."""
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DEV_JSONL = os.path.join(ROOT, "eval", "hb_os", "dev", "hb_dev.jsonl")
TEST_JSONL = os.path.join(ROOT, "eval", "hb_os", "heldout", "hb_test.jsonl")

GO_SCANNER = r"""package main

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

type Sym struct {
	ID       string `json:"id"`
	File     string `json:"file"`
	QualName string `json:"qualname"`
	Start    int    `json:"start"`
	End      int    `json:"end"`
}

func recvName(expr ast.Expr) string {
	switch t := expr.(type) {
	case *ast.Ident:
		return t.Name
	case *ast.StarExpr:
		return recvName(t.X)
	case *ast.IndexExpr:
		return recvName(t.X)
	case *ast.IndexListExpr:
		return recvName(t.X)
	}
	return ""
}

func main() {
	roots := []string{"src/cubook", "cmd/srf-cu-exec", "cmd/srf-cu-mcp", "cmd/srf-cu-snap", "cmd/srf-cu-wlcap"}
	var files []string
	for _, r := range roots {
		filepath.Walk(r, func(p string, info os.FileInfo, err error) error {
			if err == nil && !info.IsDir() && strings.HasSuffix(p, ".go") {
				files = append(files, p)
			}
			return nil
		})
	}
	sort.Strings(files)
	fset := token.NewFileSet()
	allIdents := map[string]bool{}
	stems := map[string]bool{}
	var syms []Sym
	for _, f := range files {
		base := filepath.Base(f)
		stem := strings.TrimSuffix(base, ".go")
		stems[stem] = true
		src, err := os.ReadFile(f)
		if err != nil {
			panic(err)
		}
		af, err := parser.ParseFile(fset, f, src, parser.ParseComments)
		if err != nil {
			panic(err)
		}
		ast.Inspect(af, func(n ast.Node) bool {
			if id, ok := n.(*ast.Ident); ok {
				allIdents[id.Name] = true
			}
			return true
		})
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		for _, decl := range af.Decls {
			if fd, ok := decl.(*ast.FuncDecl); ok {
				qn := fd.Name.Name
				if fd.Recv != nil && len(fd.Recv.List) > 0 {
					rn := recvName(fd.Recv.List[0].Type)
					if rn != "" {
						qn = rn + "." + fd.Name.Name
					}
				}
				st := fset.Position(fd.Pos()).Line
				en := fset.Position(fd.End()).Line
				syms = append(syms, Sym{
					ID:       f + "::" + qn,
					File:     f,
					QualName: qn,
					Start:    st,
					End:      en,
				})
			}
		}
	}
	b, _ := json.Marshal(map[string]any{"idents": allIdents, "stems": stems, "syms": syms, "files": files})
	fmt.Println(string(b))
}
"""


def split_camel(s: str) -> list[str]:
    parts = re.split(r"[_-]+", s)
    res = []
    for p in parts:
        res.extend(re.findall(r"[A-Z]+(?=[A-Z][a-z]|\d|\b)|[A-Z]?[a-z]+|\d+", p))
    return res


def extract_surface(repo_root: str = ROOT) -> dict:
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "main.go")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(GO_SCANNER)
        out = subprocess.check_output(["go", "run", p], cwd=repo_root, text=True)
    data = json.loads(out)
    forb = set()
    for k in list(data["idents"].keys()) + list(data["stems"].keys()):
        kl = k.lower().strip("_-")
        if len(kl) >= 4:
            forb.add(kl)
        for w in split_camel(k):
            wl = w.lower()
            if len(wl) >= 4:
                forb.add(wl)
    code_words = set()
    for rel in data["files"]:
        fp = os.path.join(repo_root, rel)
        with open(fp, "r", encoding="utf-8") as fh:
            txt = fh.read().lower()
            for w in re.findall(r"[a-z0-9_]+", txt):
                code_words.add(w)
                for sw in w.split("_"):
                    if sw:
                        code_words.add(sw)
    sym_map = {s["id"]: s for s in data["syms"]}
    return {
        "forbidden_tokens": forb,
        "code_words": code_words,
        "sym_map": sym_map,
        "files": data["files"],
    }


def load_jsonl(path: str) -> list[dict]:
    if not os.path.isfile(path):
        raise ValueError(f"missing file: {path}")
    rows = []
    with open(path, "r", encoding="utf-8") as fh:
        for i, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception as exc:
                raise ValueError(f"{path}:{i}: invalid JSON: {exc}") from exc
    if not rows:
        raise ValueError(f"empty jsonl file: {path}")
    return rows


def check_files(dev_path: str = DEV_JSONL, test_path: str = TEST_JSONL, surface: dict | None = None) -> list[str]:
    errors = []
    try:
        dev_rows = load_jsonl(dev_path)
    except Exception as exc:
        return [str(exc)]
    try:
        test_rows = load_jsonl(test_path)
    except Exception as exc:
        return [str(exc)]

    if surface is None:
        surface = extract_surface(ROOT)
    forb = surface["forbidden_tokens"]
    code_words = surface["code_words"]

    dev_qids = set()
    for r in dev_rows:
        qid = r.get("qid", "")
        if not qid:
            errors.append("DEV row missing qid")
        if qid in dev_qids:
            errors.append(f"duplicate DEV qid: {qid}")
        dev_qids.add(qid)
        if r.get("split") != "DEV":
            errors.append(f"E_BLIND: non-DEV split {r.get('split')!r} in DEV file ({qid})")

    test_qids = set()
    for r in test_rows:
        qid = r.get("qid", "")
        if not qid:
            errors.append("TEST row missing qid")
        if qid in test_qids:
            errors.append(f"duplicate TEST qid: {qid}")
        test_qids.add(qid)
        if qid in dev_qids:
            errors.append(f"E_BLIND: DEV qid {qid} present in TEST")
        if r.get("split") != "TEST":
            errors.append(f"E_BLIND: non-TEST split {r.get('split')!r} in TEST file ({qid})")

    for split_label, rows in [("DEV", dev_rows), ("TEST", test_rows)]:
        for r in rows:
            qid = r.get("qid", "?")
            txt = r.get("text", "")
            cls = r.get("class", "")
            if not isinstance(txt, str) or not txt.strip():
                errors.append(f"{split_label} {qid}: empty text")
                continue
            tokens = re.findall(r"[A-Za-z0-9_]+", txt)
            leaked = []
            go_hits = []
            for tok in tokens:
                tl = tok.lower().strip("_-")
                if len(tl) >= 4 and tl in forb:
                    leaked.append(tl)
                for sw in split_camel(tok):
                    swl = sw.lower()
                    if len(swl) >= 4 and swl in forb:
                        leaked.append(swl)
                if cls == "ABSENT" and len(tl) >= 4 and tl in code_words:
                    go_hits.append(tl)
            if leaked:
                errors.append(f"E_LEAK {split_label} {qid}: leaked Go identifier/stem token(s) {sorted(set(leaked))}")
            if go_hits:
                errors.append(f"E_ABSENT_HIT {split_label} {qid}: ABSENT query token(s) hit Go corpus {sorted(set(go_hits))}")

    return errors


def main() -> int:
    dev_p = sys.argv[1] if len(sys.argv) > 1 else DEV_JSONL
    test_p = sys.argv[2] if len(sys.argv) > 2 else TEST_JSONL
    errs = check_files(dev_p, test_p)
    if errs:
        for e in errs:
            print(e, file=sys.stderr)
        return 5
    print(json.dumps({"ok": True, "dev": dev_p, "test": test_p}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
