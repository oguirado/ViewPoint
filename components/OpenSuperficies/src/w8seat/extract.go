package w8seat

import (
	"archive/tar"
	"bytes"
	"encoding/json"
	"io"
	"path"
	"regexp"
	"strings"
	"unicode"
	"unicode/utf8"

	"opensuperficies.local/srf/src/w8seat/patchpolicy"
	"opensuperficies.local/srf/src/workerbridge"
)

var hexRun = regexp.MustCompile(`[a-fA-F0-9]{129,}`)
var encodedRun = regexp.MustCompile(`[A-Za-z0-9+/=]{100,}`)
var lowerHex = regexp.MustCompile(`^[a-f0-9]+$`)

func textCheck(name string, b []byte, paths []string) error {
	bad := func() error { return refusal(5, "verification", "non-text or encoded payload") }
	if len(b) == 0 || !utf8.Valid(b) || bytes.IndexByte(b, 0) >= 0 || hexRun.Match(b) {
		return bad()
	}
	for _, run := range encodedRun.FindAll(b, -1) {
		if !lowerHex.Match(run) {
			return bad()
		}
	}
	switch path.Ext(name) {
	case ".json":
		// Check the decoded strings too: escapes must not bypass encoded-data rules.
		d := json.NewDecoder(bytes.NewReader(b))
		d.UseNumber()
		var v map[string]any
		if d.Decode(&v) != nil || v == nil || d.Decode(new(any)) != io.EOF {
			return bad()
		}
		var walk func(any) bool
		walk = func(x any) bool {
			switch z := x.(type) {
			case string:
				if hexRun.MatchString(z) {
					return false
				}
				for _, r := range encodedRun.FindAllString(z, -1) {
					if !lowerHex.MatchString(r) {
						return false
					}
				}
			case map[string]any:
				for k, v := range z {
					if !walk(k) || !walk(v) {
						return false
					}
				}
			case []any:
				for _, v := range z {
					if !walk(v) {
						return false
					}
				}
			}
			return true
		}
		if !walk(v) {
			return bad()
		}
	case ".patch", ".diff":
		if _, e := patchpolicy.Check(b, paths); e != nil {
			return bad()
		}
	case ".md", ".txt":
		for _, r := range string(b) {
			if !unicode.IsPrint(r) && r != '\n' && r != '\r' && r != '\t' {
				return bad()
			}
		}
	default:
		return bad()
	}
	return nil
}

// AdmitExtract probes one byte beyond the wire cap before parsing. USTAR only
// is checked at the raw block level as tar.Reader hides extension headers.
func AdmitExtract(r io.Reader, paths []string) (map[string][]byte, error) {
	fail := func() (map[string][]byte, error) {
		return nil, refusal(5, "verification", "extract cap, format or text policy")
	}
	b, e := io.ReadAll(io.LimitReader(r, MaxArchive+1))
	if e != nil || len(b) > MaxArchive || len(b)%512 != 0 {
		return fail()
	}
	files := map[string][]byte{}
	total := 0
	offset := 0
	ended := false
	for offset+512 <= len(b) {
		block := b[offset : offset+512]
		if bytes.Equal(block, make([]byte, 512)) {
			ended = true
			break
		}
		if block[156] != tar.TypeReg || !bytes.Equal(block[257:265], []byte("ustar\x0000")) {
			return fail()
		}
		// No prefix fields or GNU/PAX extensions, even if a parser would normalize them.
		if len(bytes.Trim(block[345:500], "\x00")) != 0 {
			return fail()
		}
		tr := tar.NewReader(bytes.NewReader(b[offset:]))
		h, e := tr.Next()
		if e != nil || h.Format != tar.FormatUSTAR || len(h.PAXRecords) != 0 || h.Size <= 0 || h.Size > MaxText || len(h.Name) > 100 || h.Name != path.Base(h.Name) || h.Name == "." || h.Name == ".." || h.Name == "receipt.json" || strings.ContainsAny(h.Name, "\\\x00") || files[h.Name] != nil || len(files) >= 64 {
			return fail()
		}
		total += int(h.Size)
		if total > MaxText {
			return fail()
		}
		end := offset + 512 + int(h.Size)
		if end > len(b) {
			return fail()
		}
		data := b[offset+512 : end]
		if e := textCheck(h.Name, data, paths); e != nil {
			return nil, e
		}
		files[h.Name] = append([]byte(nil), data...)
		padded := (int(h.Size) + 511) / 512 * 512
		if offset+512+padded > len(b) || !bytes.Equal(b[end:offset+512+padded], make([]byte, offset+512+padded-end)) {
			return fail()
		}
		offset += 512 + padded
	}
	if !ended || len(files) == 0 || len(b)-offset < 1024 || !bytes.Equal(b[offset:], make([]byte, len(b)-offset)) {
		return fail()
	}
	return files, nil
}
func ExtractRecord(files map[string][]byte) map[string]any {
	n := 0
	m := map[string]any{}
	for k, v := range files {
		n += len(v)
		m[k] = hash(v)
	}
	return map[string]any{"method": "tar-exec", "entries": len(files), "bytes": n, "text_only": true, "mode": "0644", "owner_asserted": true, "staging_residue": 0, "sha256": hash(canonical(m))}
}
func validateReceipt(b []byte) error {
	v, e := workerbridge.Decode(b)
	if e != nil {
		return e
	}
	return workerbridge.ValidateDef(v, "receipt")
}
