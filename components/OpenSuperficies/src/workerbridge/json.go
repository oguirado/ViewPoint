// Package workerbridge implements the frozen W8 contract and pure bridge codecs.
// It does not launch processes, write outboxes, or grant runtime authority.
package workerbridge

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Document is an integer-only JSON object. Parse returns independent owned data.
type Document = map[string]any

// Reject carries a bridge Reject class and the W8 normalized exit code.
type Reject struct {
	Class  string
	Code   int
	Detail string
}

func (e *Reject) Error() string         { return e.Class + ": " + e.Detail }
func reject(class, detail string) error { return &Reject{Class: class, Code: 5, Detail: detail} }
func unavailable(detail string) error   { return &Reject{Class: "unavailable", Code: 4, Detail: detail} }

// ExitCode maps success to 0 and contract refusals to 4 or 5; never 1.
func ExitCode(err error) int {
	if err == nil {
		return 0
	}
	var r *Reject
	if errors.As(err, &r) && r != nil && r.Code >= 2 && r.Code <= 7 {
		return r.Code
	}
	return 5
}
func Class(err error) string {
	var r *Reject
	if errors.As(err, &r) && r != nil && r.Class != "" {
		return r.Class
	}
	if err != nil {
		return "MALFORMED"
	}
	return ""
}

var integerPattern = regexp.MustCompile(`^-?(?:0|[1-9][0-9]*)$`)

// Decode refuses ambiguous wire data before schema validation.
func Decode(data []byte) (any, error) {
	if !utf8.Valid(data) || bytes.HasPrefix(data, []byte{0xef, 0xbb, 0xbf}) {
		return nil, reject("MALFORMED", "invalid UTF-8 or BOM")
	}
	// encoding/json replaces lone UTF-16 surrogates; refuse them before decoding.
	for i := 0; i < len(data); i++ {
		if data[i] != '"' {
			if i == 0 || strings.ContainsRune(" \t\n\r[:,", rune(data[i-1])) {
				for _, constant := range []string{"NaN", "Infinity", "-Infinity"} {
					if bytes.HasPrefix(data[i:], []byte(constant)) {
						return nil, reject("NONCANONICAL_NUMBER", "non-finite number")
					}
				}
			}
			continue
		}
		i++
		for i < len(data) && data[i] != '"' {
			if data[i] != '\\' {
				i++
				continue
			}
			i++
			if i >= len(data) {
				break
			}
			if data[i] != 'u' {
				i++
				continue
			}
			if i+4 >= len(data) {
				return nil, reject("MALFORMED", "short Unicode escape")
			}
			n, e := strconv.ParseUint(string(data[i+1:i+5]), 16, 16)
			if e != nil {
				return nil, reject("MALFORMED", "Unicode escape")
			}
			i += 5
			if n >= 0xd800 && n <= 0xdbff {
				if i+5 >= len(data) || string(data[i:i+2]) != `\u` {
					return nil, reject("MALFORMED", "lone surrogate")
				}
				lo, e := strconv.ParseUint(string(data[i+2:i+6]), 16, 16)
				if e != nil || lo < 0xdc00 || lo > 0xdfff {
					return nil, reject("MALFORMED", "lone surrogate")
				}
				i += 6
			} else if n >= 0xdc00 && n <= 0xdfff {
				return nil, reject("MALFORMED", "lone surrogate")
			}
		}
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	v, e := readValue(d, 0)
	if e != nil {
		return nil, e
	}
	if _, e = d.Token(); e != io.EOF {
		return nil, reject("MALFORMED", "trailing JSON data")
	}
	return v, nil
}
func readValue(d *json.Decoder, depth int) (any, error) {
	if depth > 128 {
		return nil, reject("MALFORMED", "JSON nesting limit")
	}
	token, e := d.Token()
	if e != nil {
		return nil, reject("MALFORMED", "invalid JSON")
	}
	switch t := token.(type) {
	case json.Number:
		if !integerPattern.MatchString(string(t)) || t == "-0" {
			return nil, reject("NONCANONICAL_NUMBER", "integer-only JSON")
		}
		return t, nil
	case json.Delim:
		if t == '{' {
			o := Document{}
			for d.More() {
				k, e := d.Token()
				if e != nil {
					return nil, reject("MALFORMED", "object key")
				}
				s, ok := k.(string)
				if !ok {
					return nil, reject("MALFORMED", "object key")
				}
				if _, ok = o[s]; ok {
					return nil, reject("DUPLICATE_KEY", "duplicate object key")
				}
				v, e := readValue(d, depth+1)
				if e != nil {
					return nil, e
				}
				o[s] = v
			}
			if _, e = d.Token(); e != nil {
				return nil, reject("MALFORMED", "object terminator")
			}
			return o, nil
		}
		if t == '[' {
			a := []any{}
			for d.More() {
				v, e := readValue(d, depth+1)
				if e != nil {
					return nil, e
				}
				a = append(a, v)
			}
			if _, e = d.Token(); e != nil {
				return nil, reject("MALFORMED", "array terminator")
			}
			return a, nil
		}
		return nil, reject("MALFORMED", "unexpected delimiter")
	default:
		return token, nil
	}
}

// Parse decodes a strict JSON object.
func Parse(data []byte) (Document, error) {
	v, e := Decode(data)
	if e != nil {
		return nil, e
	}
	o, ok := v.(map[string]any)
	if !ok {
		return nil, reject("MALFORMED", "expected object")
	}
	return o, nil
}

// Canonical uses the worker binding format: sorted UTF-8 keys, compact JSON,
// no HTML escaping, integer-only numbers and Python-compatible control escapes.
func Canonical(v any) ([]byte, error) { return canonical(v, false) }

// BridgeCanonical uses the canon bridge's C0-as-u00xx encoding.
func BridgeCanonical(v any) ([]byte, error) { return canonical(v, true) }
func canonical(v any, bridge bool) ([]byte, error) {
	switch v.(type) {
	case float32, float64:
		return nil, reject("NONCANONICAL_NUMBER", "float value")
	case nil, bool, string, json.Number, map[string]any, []any:
	default:
		// Normalize public typed values once, preserving integers through UseNumber.
		b, e := json.Marshal(v)
		if e != nil {
			return nil, reject("MALFORMED", "unsupported JSON value")
		}
		v, e = Decode(b)
		if e != nil {
			return nil, e
		}
	}
	var b bytes.Buffer
	if e := encode(&b, v, bridge, 0); e != nil {
		return nil, e
	}
	return b.Bytes(), nil
}
func encode(b *bytes.Buffer, v any, bridge bool, depth int) error {
	if depth > 128 {
		return reject("MALFORMED", "JSON nesting limit")
	}
	switch x := v.(type) {
	case float32, float64:
		return reject("NONCANONICAL_NUMBER", "float value")
	case nil:
		b.WriteString("null")
	case bool:
		if x {
			b.WriteString("true")
		} else {
			b.WriteString("false")
		}
	case json.Number:
		if !integerPattern.MatchString(string(x)) || x == "-0" {
			return reject("NONCANONICAL_NUMBER", "integer-only JSON")
		}
		b.WriteString(string(x))
	case string:
		if !utf8.ValidString(x) {
			return reject("MALFORMED", "invalid UTF-8")
		}
		b.WriteByte('"')
		for _, r := range x {
			switch {
			case r == '"' || r == '\\':
				b.WriteByte('\\')
				b.WriteRune(r)
			case r < 32:
				short := map[rune]byte{'\b': 'b', '\f': 'f', '\n': 'n', '\r': 'r', '\t': 't'}
				if c, ok := short[r]; ok && !bridge {
					b.WriteByte('\\')
					b.WriteByte(c)
				} else {
					fmt.Fprintf(b, `\u%04x`, r)
				}
			default:
				b.WriteRune(r)
			}
		}
		b.WriteByte('"')
	case map[string]any:
		if x == nil {
			b.WriteString("null")
			return nil
		}
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		b.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				b.WriteByte(',')
			}
			if e := encode(b, k, bridge, depth+1); e != nil {
				return e
			}
			b.WriteByte(':')
			if e := encode(b, x[k], bridge, depth+1); e != nil {
				return e
			}
		}
		b.WriteByte('}')
	case []any:
		if x == nil {
			b.WriteString("null")
			return nil
		}
		b.WriteByte('[')
		for i, v := range x {
			if i > 0 {
				b.WriteByte(',')
			}
			if e := encode(b, v, bridge, depth+1); e != nil {
				return e
			}
		}
		b.WriteByte(']')
	default:
		rv := reflect.ValueOf(v)
		if rv.IsValid() && (rv.Kind() >= reflect.Int && rv.Kind() <= reflect.Int64) {
			b.WriteString(strconv.FormatInt(rv.Int(), 10))
			return nil
		}
		raw, e := json.Marshal(v)
		if e != nil {
			return reject("MALFORMED", "unsupported value")
		}
		n, e := Decode(raw)
		if e != nil {
			return e
		}
		return encode(b, n, bridge, depth+1)
	}
	return nil
}
func equal(a, b any) bool {
	x, e := Canonical(a)
	y, f := Canonical(b)
	return e == nil && f == nil && bytes.Equal(x, y)
}
func digest(v any) string   { b, _ := Canonical(v); return hash(b) }
func hash(b []byte) string  { h := sha256.Sum256(b); return hex.EncodeToString(h[:]) }
func object(v any) Document { o, _ := v.(map[string]any); return o }
func array(v any) []any     { a, _ := v.([]any); return a }
func str(v any) string      { s, _ := v.(string); return s }
func num(v any) int64       { n, _ := strconv.ParseInt(fmt.Sprint(v), 10, 64); return n }
func contains(a any, v any) bool {
	for _, x := range array(a) {
		if equal(x, v) {
			return true
		}
	}
	return false
}
func clone(v Document) Document { b, _ := Canonical(v); d, _ := Parse(b); return d }
func cleanPath(p string) bool {
	if p == "" || strings.HasPrefix(p, "/") || strings.ContainsAny(p, "\\\x00") {
		return false
	}
	for _, s := range strings.Split(p, "/") {
		if s == "" || s == "." || s == ".." {
			return false
		}
	}
	return true
}
