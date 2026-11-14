package main

import (
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"
)

// Capture only a bounded last line. Oversize lines are omitted rather than
// retaining a tail that could lose the marker identifying sensitive content.
type programStderr struct {
	mu       sync.Mutex
	line     []byte
	last     string
	overflow bool
}

func (s *programStderr) Write(p []byte) (int, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, b := range p {
		if b == '\n' {
			if s.overflow {
				s.last = "program stderr line omitted"
			} else if v := strings.TrimSpace(string(s.line)); v != "" {
				s.last = v
			}
			s.line = nil
			s.overflow = false
		} else if !s.overflow {
			if len(s.line) >= 4096 {
				s.line = nil
				s.overflow = true
			} else {
				s.line = append(s.line, b)
			}
		}
	}
	return len(p), nil
}
func (s *programStderr) reason() string {
	s.mu.Lock()
	defer s.mu.Unlock()
	reason := s.last
	if s.overflow {
		reason = "program stderr line omitted"
	} else if v := strings.TrimSpace(string(s.line)); v != "" {
		reason = v
	}
	if reason == "" {
		reason = "program exited"
	}
	// Diagnostics containing credential markers are withheld in full, before
	// truncation. Never put arbitrary exec error text or environment on the wire.
	lower := strings.ToLower(reason)
	for _, marker := range []string{"password", "passwd", "token", "secret", "cookie", "authorization", "credential", "private key", "://", "bearer ", "api_key", "apikey", "auth.json", ".key", "="} {
		if strings.Contains(lower, marker) {
			reason = "program stderr redacted"
			break
		}
	}
	reason = strings.Map(func(r rune) rune {
		if unicode.IsControl(r) || !unicode.IsPrint(r) {
			return ' '
		}
		return r
	}, strings.ToValidUTF8(reason, "?"))
	// Bound the entire SRF1 ERR line (excluding newline) to 200 bytes.
	const limit = 200 - len("SRF1 ERR ")
	if len(reason) > limit {
		reason = reason[:limit]
		for !utf8.ValidString(reason) {
			reason = reason[:len(reason)-1]
		}
	}
	return reason
}
