package mcp

import (
	"os"
	"path/filepath"
)

// CAS is a content-addressed store for untrusted server bytes.
type CAS struct{ Dir string }

// Put stores b and returns (ref, sha). Over MaxContent -> rc 6, nothing written.
func (c *CAS) Put(b []byte) (string, string, error) {
	if len(b) > MaxContent {
		return "", "", budget("content_over_256kib")
	}
	sum := hexSum(b)
	if err := os.MkdirAll(c.Dir, 0o700); err != nil {
		return "", "", err
	}
	p := filepath.Join(c.Dir, sum)
	if _, err := os.Stat(p); err != nil {
		if err := os.WriteFile(p, b, 0o600); err != nil {
			return "", "", err
		}
	}
	return "cas:sha256:" + sum, sum, nil
}
