package workerbridge

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"math/big"
	"regexp"
	"strings"
	"sync"
	"unicode/utf8"
)

//go:embed worker.schema.json
var schemaBytes []byte
var schemaOnce sync.Once
var contract Document
var contractErr error
var patterns map[string]func(string) bool

// validate implements only the audited keyword subset in the frozen v3 file.
// Unknown keywords/references/patterns fail closed; it is not a general schema API.
func validate(v any, def string) error {
	schemaOnce.Do(func() {
		contract, contractErr = Parse(schemaBytes)
		patterns = map[string]func(string) bool{}
		if contractErr == nil {
			contractErr = audit(contract, 0)
		}
	})
	if contractErr != nil {
		return contractErr
	}
	var schema any = contract
	ok := true
	if def != "" {
		schema, ok = object(contract["$defs"])[def]
	}
	if !ok {
		return reject("MALFORMED", "unknown contract definition")
	}
	errs := checkSchema(v, schema, 0)
	for _, e := range errs {
		if Class(e) == "UNKNOWN_KEY" {
			return e
		}
	}
	if len(errs) > 0 {
		return errs[0]
	}
	return nil
}
func audit(v any, depth int) error {
	if depth > 128 {
		return reject("MALFORMED", "schema depth")
	}
	if _, ok := v.(bool); ok {
		return nil
	}
	s, ok := v.(map[string]any)
	if !ok {
		return reject("MALFORMED", "schema object")
	}
	allowed := strings.Fields("$schema $id title description $comment $defs $ref type properties required additionalProperties items minItems maxItems uniqueItems minLength maxLength minimum maximum pattern const enum oneOf allOf if then else")
	for key := range s {
		found := false
		for _, a := range allowed {
			if key == a {
				found = true
			}
		}
		if !found {
			return reject("MALFORMED", "unsupported schema keyword")
		}
	}
	if r, ok := s["$ref"]; ok {
		if !strings.HasPrefix(str(r), "#/$defs/") || object(contract["$defs"])[strings.TrimPrefix(str(r), "#/$defs/")] == nil {
			return reject("MALFORMED", "unresolved schema reference")
		}
	}
	if p, ok := s["pattern"]; ok {
		fn, e := compilePattern(str(p))
		if e != nil {
			return e
		}
		patterns[str(p)] = fn
	}
	for _, k := range []string{"properties", "$defs"} {
		for _, c := range object(s[k]) {
			if e := audit(c, depth+1); e != nil {
				return e
			}
		}
	}
	for _, k := range []string{"items", "if", "then", "else"} {
		if c, ok := s[k]; ok {
			if e := audit(c, depth+1); e != nil {
				return e
			}
		}
	}
	for _, k := range []string{"oneOf", "allOf"} {
		for _, c := range array(s[k]) {
			if e := audit(c, depth+1); e != nil {
				return e
			}
		}
	}
	return nil
}
func compilePattern(p string) (func(string) bool, error) {
	original := p
	extra := func(string) bool { return true }
	// RE2 lacks lookahead. These are the exact v3 patterns; assertions become
	// component checks, with the consuming part still matched in full by RE2.
	switch {
	case strings.HasPrefix(p, `^(?!(?:.*/)?\.git`):
		p = `^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*$`
		extra = func(s string) bool {
			if !cleanPath(s) {
				return false
			}
			for _, c := range strings.Split(s, "/") {
				if c == ".git" {
					return false
				}
			}
			return true
		}
	case strings.HasPrefix(p, `^/dev/shm/(?!`):
		p = `^/dev/shm/[A-Za-z0-9_-][A-Za-z0-9_.-]*(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*$`
	case strings.HasPrefix(p, `^https://`):
		p = strings.ReplaceAll(p, `(?!\.\.?(/|$))`, "")
		extra = func(s string) bool {
			parts := strings.SplitN(strings.TrimPrefix(s, "https://"), "/", 2)
			return len(parts) == 2 && cleanPath(parts[1])
		}
	case strings.HasPrefix(p, `^(?!refs/)`):
		p = `^[A-Za-z0-9][A-Za-z0-9._/-]*[A-Za-z0-9]$`
		extra = func(s string) bool {
			if strings.HasPrefix(s, "refs/") || strings.Contains(s, "..") || strings.Contains(s, "@{") || strings.Contains(s, "//") || strings.HasSuffix(s, ".") {
				return false
			}
			for _, c := range strings.Split(s, "/") {
				if strings.HasPrefix(c, ".") || strings.HasSuffix(c, ".lock") {
					return false
				}
			}
			return true
		}
	case strings.HasPrefix(p, `^/workspace(?:/(?!`):
		p = `^/workspace(?:/[A-Za-z0-9_-][A-Za-z0-9_.-]*)+$`
	}
	if strings.Contains(p, "(?=") || strings.Contains(p, "(?!") {
		return nil, reject("MALFORMED", "unsupported schema assertion")
	}
	u := regexp.MustCompile(`\\u([0-9a-fA-F]{4})`)
	p = u.ReplaceAllString(p, `\x{$1}`)
	rx, e := regexp.Compile(p)
	if e != nil {
		return nil, reject("MALFORMED", "invalid schema pattern: "+original)
	}
	return func(s string) bool {
		loc := rx.FindStringIndex(s)
		return loc != nil && loc[0] == 0 && loc[1] == len(s) && extra(s)
	}, nil
}
func checkSchema(v, raw any, depth int) []error {
	bad := func(s string) []error { return []error{reject("MALFORMED", s)} }
	if depth > 128 {
		return bad("schema recursion limit")
	}
	if b, ok := raw.(bool); ok {
		if b {
			return nil
		}
		return bad("false schema")
	}
	s := object(raw)
	var errs []error
	if r, ok := s["$ref"]; ok {
		errs = append(errs, checkSchema(v, object(contract["$defs"])[strings.TrimPrefix(str(r), "#/$defs/")], depth+1)...)
	}
	if t, ok := s["type"]; ok {
		valid := false
		switch t {
		case "object":
			_, valid = v.(map[string]any)
		case "array":
			_, valid = v.([]any)
		case "string":
			_, valid = v.(string)
		case "integer":
			_, valid = v.(json.Number)
		case "boolean":
			_, valid = v.(bool)
		case "null":
			valid = v == nil
		}
		if !valid {
			return append(errs, bad("expected "+str(t))...)
		}
	}
	if c, ok := s["const"]; ok && !equal(v, c) {
		errs = append(errs, bad("const mismatch")...)
	}
	if options, ok := s["enum"]; ok && !contains(options, v) {
		errs = append(errs, bad("enum mismatch")...)
	}
	switch x := v.(type) {
	case map[string]any:
		props := object(s["properties"])
		for _, k := range array(s["required"]) {
			if _, ok := x[str(k)]; !ok {
				errs = append(errs, bad("missing required property")...)
			}
		}
		for k, v := range x {
			if child, ok := props[k]; ok {
				errs = append(errs, checkSchema(v, child, depth+1)...)
			} else if s["additionalProperties"] == false {
				errs = append(errs, reject("UNKNOWN_KEY", "unknown property"))
			}
		}
	case []any:
		if n, ok := s["minItems"]; ok && len(x) < int(num(n)) {
			errs = append(errs, bad("array too short")...)
		}
		if n, ok := s["maxItems"]; ok && len(x) > int(num(n)) {
			errs = append(errs, bad("array too long")...)
		}
		seen := map[string]bool{}
		for _, c := range x {
			if s["uniqueItems"] == true {
				b, _ := Canonical(c)
				if seen[string(b)] {
					errs = append(errs, bad("duplicate item")...)
				}
				seen[string(b)] = true
			}
			if child, ok := s["items"]; ok {
				errs = append(errs, checkSchema(c, child, depth+1)...)
			}
		}
	case string:
		n := utf8.RuneCountInString(x)
		if m, ok := s["minLength"]; ok && n < int(num(m)) {
			errs = append(errs, bad("string too short")...)
		}
		if m, ok := s["maxLength"]; ok && n > int(num(m)) {
			errs = append(errs, bad("string too long")...)
		}
		if p, ok := s["pattern"]; ok && !patterns[str(p)](x) {
			errs = append(errs, bad("pattern mismatch")...)
		}
	case json.Number:
		n, ok := new(big.Int).SetString(string(x), 10)
		if !ok {
			return bad("integer expected")
		}
		for _, k := range []string{"minimum", "maximum"} {
			if bound, ok := s[k]; ok {
				b, _ := new(big.Int).SetString(fmt.Sprint(bound), 10)
				cmp := n.Cmp(b)
				if k == "minimum" && cmp < 0 || k == "maximum" && cmp > 0 {
					errs = append(errs, bad("integer range")...)
				}
			}
		}
	}
	for _, c := range array(s["allOf"]) {
		errs = append(errs, checkSchema(v, c, depth+1)...)
	}
	if choices, ok := s["oneOf"]; ok {
		matches := 0
		var failures []error
		for _, c := range array(choices) {
			es := checkSchema(v, c, depth+1)
			if len(es) == 0 {
				matches++
			} else {
				failures = append(failures, es...)
			}
		}
		if matches != 1 {
			errs = append(errs, bad("oneOf mismatch")...)
			errs = append(errs, failures...)
		}
	}
	if condition, ok := s["if"]; ok {
		branch := "else"
		if len(checkSchema(v, condition, depth+1)) == 0 {
			branch = "then"
		}
		if c, ok := s[branch]; ok {
			errs = append(errs, checkSchema(v, c, depth+1)...)
		}
	}
	return errs
}
