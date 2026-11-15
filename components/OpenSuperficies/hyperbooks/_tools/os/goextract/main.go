// goextract: tooling-only Go symbol extractor for the OS hyperbook gates (stdlib go/parser, go/printer).
// Emits one hb.symbols.bundle/v1 JSON (canonical: compact, sorted keys, LF) so hbtools_v2 can consume Go symbols.
//
//	goextract --root R --dirs a,b,c [--hb-id ID] [--out FILE]   (stdout when --out is empty)
//
// Exit codes: 0 ok, 2 usage, 5 failure (never 1).
package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/printer"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
)

const schema = "hb.symbols.bundle/v1"

type File struct {
	Path   string `json:"path"`
	Lang   string `json:"lang"`
	Sha256 string `json:"sha256"`
}

type Symbol struct {
	ID               string `json:"id"`
	Path             string `json:"path"`
	Name             string `json:"name"`
	Kind             string `json:"kind"`
	Span             [2]int `json:"span"`
	Lang             string `json:"lang"`
	Signature        string `json:"signature"`
	SignatureSha256  string `json:"signature_sha256"`
	Receiver         string `json:"receiver,omitempty"`
	PkgPath          string `json:"pkg_path"`
	Exported         bool   `json:"exported"`
	IsTest           bool   `json:"is_test"`
}

type Span struct {
	StartLine int `json:"start_line"`
	EndLine   int `json:"end_line"`
}

type Anchor struct {
	Schema          string  `json:"schema"`
	ID              string  `json:"id"`
	Path            string  `json:"path"`
	Symbol          string  `json:"symbol"`
	Role            string  `json:"role"`
	Kind            string  `json:"kind"`
	Lang            string  `json:"lang"`
	Span            Span    `json:"span"`
	StartByte       int     `json:"start_byte"`
	EndByte         int     `json:"end_byte"`
	Bytes           int     `json:"bytes"`
	Sha256          string  `json:"sha256"`
	Sha256AST       string  `json:"sha256_ast"`
	DocSha256       string  `json:"doc_sha256"`
	Sha256Shell     string  `json:"sha256_shell,omitempty"`
	SignatureSha256 string  `json:"signature_sha256"`
	Status          string  `json:"status"`
	RedirectTo      *string `json:"redirect_to"`
}

type Edge struct {
	Src  string `json:"src"`
	Dst  string `json:"dst"`
	Kind string `json:"kind"`
}

type Text struct {
	Doc string `json:"doc"`
	Com string `json:"com"`
	Lit string `json:"lit"`
}

type Bundle struct {
	Schema      string          `json:"schema"`
	HbID        string          `json:"hb_id"`
	Root        string          `json:"root"`
	LangDefault string          `json:"lang_default"`
	Files       []File          `json:"files"`
	Symbols     []Symbol        `json:"symbols"`
	Anchors     []Anchor        `json:"anchors"`
	Edges       []Edge          `json:"edges"`
	Texts       map[string]Text `json:"texts"`
}

func sha(b []byte) string {
	h := sha256.Sum256(b)
	return hex.EncodeToString(h[:])
}

func printNode(fset *token.FileSet, n any) string {
	var buf bytes.Buffer
	cfg := printer.Config{Mode: printer.UseSpaces | printer.TabIndent, Tabwidth: 8}
	if err := cfg.Fprint(&buf, fset, n); err != nil {
		return "ERR:" + err.Error()
	}
	return buf.String()
}

// stripInline nils every Field doc/comment inside a spec so inline comments never move sig or sha256_ast (ADC-W5A-02a).
func stripInline(n ast.Node) {
	ast.Inspect(n, func(x ast.Node) bool {
		if f, ok := x.(*ast.Field); ok {
			f.Doc, f.Comment = nil, nil
		}
		return true
	})
}

func recvName(e ast.Expr) string {
	switch t := e.(type) {
	case *ast.StarExpr:
		return recvName(t.X)
	case *ast.IndexExpr:
		return recvName(t.X)
	case *ast.IndexListExpr:
		return recvName(t.X)
	case *ast.ParenExpr:
		return recvName(t.X)
	case *ast.Ident:
		return t.Name
	}
	return "_"
}

func docText(g *ast.CommentGroup) string {
	if g == nil {
		return ""
	}
	return strings.TrimSpace(g.Text())
}

type unit struct {
	qual, kind, sig string
	start, end      token.Pos
	node            any // printed for sha256_ast (doc stripped)
	doc             *ast.CommentGroup
	recv            string
	body            ast.Node // literals are collected here
}

func collectFile(root, rel, pkgPath string, data []byte, fset *token.FileSet, b *Bundle) error {
	f, err := parser.ParseFile(fset, rel, data, parser.ParseComments)
	if err != nil {
		return err
	}
	isTest := strings.HasSuffix(rel, "_test.go")
	tf := fset.File(f.Pos())
	offs := make([]int, tf.LineCount()+1) // offs[k-1] = byte offset of line k; offs[n] = len
	for i := 1; i <= tf.LineCount(); i++ {
		offs[i-1] = tf.Offset(tf.LineStart(i))
	}
	offs[tf.LineCount()] = len(data)
	line := func(p token.Pos) int { return fset.Position(p).Line }

	var units []unit
	inits := 0
	for _, d := range f.Decls {
		switch x := d.(type) {
		case *ast.FuncDecl:
			u := unit{kind: "function", start: x.Pos(), end: x.End(), doc: x.Doc, body: x}
			name := x.Name.Name
			if name == "init" && x.Recv == nil {
				inits++
				if inits > 1 {
					name = "init_" + strconv.Itoa(inits)
				}
			}
			u.qual = name
			if x.Recv != nil && len(x.Recv.List) > 0 {
				u.recv = recvName(x.Recv.List[0].Type)
				u.qual = u.recv + "." + name
				u.kind = "method"
			} else if isTest && strings.HasPrefix(name, "Test") {
				u.kind = "test"
			}
			cp := *x
			cp.Doc = nil
			cp.Body = nil
			u.sig = strings.Join(strings.Fields(printNode(fset, &cp)), " ")
			full := *x
			full.Doc = nil
			u.node = &full
			units = append(units, u)
		case *ast.GenDecl:
			if x.Tok != token.TYPE && x.Tok != token.CONST && x.Tok != token.VAR {
				continue
			}
			grouped := x.Lparen.IsValid()
			for _, sp := range x.Specs {
				switch s := sp.(type) {
				case *ast.TypeSpec:
					u := unit{qual: s.Name.Name, kind: "class", doc: s.Doc, body: s}
					if u.doc == nil && !grouped {
						u.doc = x.Doc
					}
					u.start, u.end = s.Pos(), s.End()
					if !grouped {
						u.start = x.Pos()
					}
					cp := *s
					cp.Doc, cp.Comment = nil, nil
					stripInline(&cp)
					u.node = &cp
					u.sig = strings.Join(strings.Fields(printNode(fset, &cp)), " ")
					if len(u.sig) > 160 {
						u.sig = u.sig[:160]
					}
					units = append(units, u)
				case *ast.ValueSpec:
					for i, n := range s.Names {
						if n.Name == "_" {
							continue
						}
						u := unit{qual: n.Name, kind: "constant", doc: s.Doc, body: s}
						if u.doc == nil && !grouped {
							u.doc = x.Doc
						}
						u.start, u.end = s.Pos(), s.End()
						if !grouped {
							u.start = x.Pos()
						}
						cp := *s
						cp.Doc, cp.Comment = nil, nil
						stripInline(&cp)
						if len(s.Names) > 1 { // keep the per-name hash independent of siblings
							cp.Names = []*ast.Ident{n}
							if len(s.Values) == len(s.Names) {
								cp.Values = []ast.Expr{s.Values[i]}
							}
						}
						u.node = &cp
						u.sig = n.Name
						units = append(units, u)
					}
				}
			}
		}
	}

	// module symbol
	nl := tf.LineCount()
	if nl < 1 {
		nl = 1
	}
	modID := rel + "::__module__"
	modDoc := docText(f.Doc)
	shell := &ast.File{Name: f.Name}
	for _, d := range f.Decls {
		if g, ok := d.(*ast.GenDecl); ok && g.Tok == token.IMPORT {
			shell.Decls = append(shell.Decls, g)
		}
	}
	fcp := *f
	fcp.Doc, fcp.Comments = nil, nil
	b.Symbols = append(b.Symbols, Symbol{ID: modID, Path: rel, Name: strings.TrimSuffix(filepath.Base(rel), ".go"), Kind: "module", Span: [2]int{1, nl}, Lang: "go",
		Signature: rel, SignatureSha256: sha([]byte(rel)), PkgPath: pkgPath, Exported: true, IsTest: isTest})
	b.Anchors = append(b.Anchors, Anchor{Schema: "hb.anchor.v2", ID: modID, Path: rel, Symbol: "__module__", Role: "symbol", Kind: "module", Lang: "go",
		Span: Span{1, nl}, StartByte: 0, EndByte: len(data), Bytes: len(data), Sha256: sha(data), Sha256AST: sha([]byte(printNode(fset, &fcp))),
		DocSha256: sha([]byte(modDoc)), Sha256Shell: sha([]byte(printNode(fset, shell))), SignatureSha256: sha([]byte(rel)), Status: "active"})
	b.Texts[modID] = Text{Doc: modDoc}

	// comments per symbol (innermost span)
	type span struct{ s, e int }
	spans := make([]span, len(units))
	for i, u := range units {
		spans[i] = span{line(u.start), line(u.end)}
	}
	for i, u := range units {
		sl, el := spans[i].s, spans[i].e
		id := rel + "::" + u.qual
		start, end := offs[sl-1], offs[el]
		raw := data[start:end]
		sigH := sha([]byte(u.sig))
		exported := ast.IsExported(u.qual[strings.LastIndex(u.qual, ".")+1:]) && (u.recv == "" || ast.IsExported(u.recv))
		b.Symbols = append(b.Symbols, Symbol{ID: id, Path: rel, Name: u.qual, Kind: u.kind, Span: [2]int{sl, el}, Lang: "go", Signature: u.sig,
			SignatureSha256: sigH, Receiver: u.recv, PkgPath: pkgPath, Exported: exported, IsTest: isTest})
		a := Anchor{Schema: "hb.anchor.v2", ID: id, Path: rel, Symbol: u.qual, Role: "symbol", Kind: u.kind, Lang: "go", Span: Span{sl, el},
			StartByte: start, EndByte: end, Bytes: len(raw), Sha256: sha(raw), Sha256AST: sha([]byte(printNode(fset, u.node))),
			DocSha256: sha([]byte(docText(u.doc))), SignatureSha256: sigH, Status: "active"}
		if u.kind == "class" {
			a.Sha256Shell = a.Sha256AST
		}
		b.Anchors = append(b.Anchors, a)
		var coms []string
		for _, cg := range f.Comments {
			cl := line(cg.Pos())
			if cl < sl || cl > el || cg == u.doc {
				continue
			}
			inner := false
			for j := range units {
				if j != i && spans[j].s <= cl && cl <= spans[j].e && spans[j].e-spans[j].s < el-sl {
					inner = true
					break
				}
			}
			if !inner {
				coms = append(coms, docText(cg))
			}
		}
		var lits []string
		ast.Inspect(u.body, func(n ast.Node) bool {
			if bl, ok := n.(*ast.BasicLit); ok && bl.Kind == token.STRING {
				if s, err := strconv.Unquote(bl.Value); err == nil && len(s) > 0 && len(s) < 80 {
					lits = append(lits, s)
				}
			}
			return true
		})
		b.Texts[id] = Text{Doc: docText(u.doc), Com: strings.Join(coms, " "), Lit: strings.Join(lits, " ")}
	}
	return nil
}

// Extract walks dirs (recursively, *.go) under root and returns the canonical bundle.
func Extract(root string, dirs []string, hbID string) (*Bundle, error) {
	b := &Bundle{Schema: schema, HbID: hbID, Root: ".", LangDefault: "go", Files: []File{}, Symbols: []Symbol{}, Anchors: []Anchor{}, Edges: []Edge{}, Texts: map[string]Text{}}
	var rels []string
	for _, d := range dirs {
		base := filepath.Join(root, d)
		st, err := os.Stat(base)
		if err != nil {
			return nil, err
		}
		if !st.IsDir() {
			rels = append(rels, filepath.ToSlash(d))
			continue
		}
		err = filepath.WalkDir(base, func(p string, de os.DirEntry, err error) error {
			if err != nil {
				return err
			}
			if !de.IsDir() && strings.HasSuffix(p, ".go") {
				r, _ := filepath.Rel(root, p)
				rels = append(rels, filepath.ToSlash(r))
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Strings(rels)
	seen := map[string]bool{}
	for _, rel := range rels {
		if seen[rel] {
			continue
		}
		seen[rel] = true
		data, err := os.ReadFile(filepath.Join(root, rel))
		if err != nil {
			return nil, err
		}
		b.Files = append(b.Files, File{Path: rel, Lang: "go", Sha256: sha(data)})
		if err := collectFile(root, rel, filepath.ToSlash(filepath.Dir(rel)), data, token.NewFileSet(), b); err != nil {
			return nil, fmt.Errorf("%s: %w", rel, err)
		}
	}
	sort.Slice(b.Symbols, func(i, j int) bool { return b.Symbols[i].ID < b.Symbols[j].ID })
	sort.Slice(b.Anchors, func(i, j int) bool { return b.Anchors[i].ID < b.Anchors[j].ID })
	ids := map[string]bool{}
	for _, s := range b.Symbols {
		if ids[s.ID] {
			return nil, fmt.Errorf("E_DUP_ID %s", s.ID)
		}
		ids[s.ID] = true
	}
	return b, nil
}

// Canon returns the canonical bytes of the bundle.
func Canon(b *Bundle) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(b); err != nil {
		return nil, err
	}
	return buf.Bytes(), nil
}

func run(args []string, stdout, stderr *os.File) int {
	fs := flag.NewFlagSet("goextract", flag.ContinueOnError)
	fs.SetOutput(stderr)
	root := fs.String("root", ".", "repository root")
	dirs := fs.String("dirs", "", "comma separated dirs/files under root")
	hbID := fs.String("hb-id", "HB-OSCUBOOK-001", "book id")
	out := fs.String("out", "", "output file (stdout when empty)")
	if err := fs.Parse(args); err != nil || *dirs == "" || fs.NArg() != 0 {
		fmt.Fprintln(stderr, "usage: goextract --root R --dirs a,b [--hb-id ID] [--out FILE]")
		return 2
	}
	b, err := Extract(*root, strings.Split(*dirs, ","), *hbID)
	if err != nil {
		fmt.Fprintln(stderr, "goextract:", err)
		return 5
	}
	raw, err := Canon(b)
	if err != nil {
		fmt.Fprintln(stderr, "goextract:", err)
		return 5
	}
	if *out == "" {
		stdout.Write(raw)
		return 0
	}
	if err := os.MkdirAll(filepath.Dir(*out), 0o755); err != nil {
		fmt.Fprintln(stderr, "goextract:", err)
		return 5
	}
	if err := os.WriteFile(*out, raw, 0o644); err != nil {
		fmt.Fprintln(stderr, "goextract:", err)
		return 5
	}
	return 0
}

func main() { os.Exit(run(os.Args[1:], os.Stdout, os.Stderr)) }
