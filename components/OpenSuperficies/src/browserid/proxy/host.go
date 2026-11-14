package proxy

import (
	"errors"
	"net"
	"strings"
	"unicode"
	"unicode/utf8"
)

var errHost = errors.New("invalid host")

// NormalizeHost implements a deliberately strict IDNA2008 subset: Unicode
// supported letter labels are converted to A-labels; combining and RTL labels are
// refused rather than guessed without Unicode normalization/bidi tables.
func NormalizeHost(host string) (string, error) {
	if len(host) > 1024 {
		return "", errHost
	}
	if !utf8.ValidString(host) || strings.ContainsAny(host, "/@\\?#%[]:\x00\r\n\t ") { // IPv6 is handled separately.
		if ip := net.ParseIP(host); ip != nil {
			return ip.String(), nil
		}
		return "", errHost
	}
	host = strings.TrimSuffix(strings.ToLower(host), ".")
	if ip := net.ParseIP(host); ip != nil {
		return ip.String(), nil
	}
	labels := strings.Split(host, ".")
	if ambiguousIP(labels) {
		return "", errHost
	}
	for i, s := range labels {
		if s == "" {
			return "", errHost
		}
		if strings.HasPrefix(s, "xn--") {
			r, e := decodePuny(s[4:])
			if e != nil {
				return "", errHost
			}
			enc, e := label(string(r))
			if e != nil || enc != s {
				return "", errHost
			}
			labels[i] = enc
		} else {
			v, e := label(s)
			if e != nil {
				return "", e
			}
			labels[i] = v
		}
	}
	result := strings.Join(labels, ".")
	if len(result) > 253 {
		return "", errHost
	}
	return result, nil
}
func label(s string) (string, error) {
	rr := []rune(s)
	if len(rr) > 63 {
		return "", errHost
	}
	if len(rr) == 0 || rr[0] == '-' || rr[len(rr)-1] == '-' {
		return "", errHost
	}
	if len(rr) >= 4 && rr[2] == '-' && rr[3] == '-' {
		return "", errHost
	}
	ascii := true
	for _, r := range rr {
		if r > 127 {
			ascii = false
			if !supportedLetter(r) {
				return "", errHost
			}
			if unicode.In(r, unicode.Arabic, unicode.Hebrew) || r >= 0xff00 && r <= 0xffef {
				return "", errHost
			}
		} else if !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9' || r == '-') {
			return "", errHost
		}
	}
	if !ascii {
		s = "xn--" + encodePuny(rr)
	}
	if len(s) > 63 {
		return "", errHost
	}
	return s, nil
}
func adapt(d int64, n int64, first bool) int64 {
	if first {
		d /= 700
	} else {
		d /= 2
	}
	d += d / n
	k := int64(0)
	for d > 455 {
		d /= 35
		k += 36
	}
	return k + 36*d/(d+38)
}
func digit(n int64) byte {
	if n < 26 {
		return byte('a' + n)
	}
	return byte('0' + n - 26)
}
func value(r byte) int64 {
	if r >= 'a' && r <= 'z' {
		return int64(r - 'a')
	}
	if r >= '0' && r <= '9' {
		return int64(r - '0' + 26)
	}
	return -1
}
func threshold(k, bias int64) int64 {
	if k <= bias {
		return 1
	}
	if k >= bias+26 {
		return 26
	}
	return k - bias
}
func encodePuny(rr []rune) string {
	out := []byte{}
	for _, r := range rr {
		if r < 128 {
			out = append(out, byte(r))
		}
	}
	b := len(out)
	h := b
	if b > 0 {
		out = append(out, '-')
	}
	n, delta, bias := int64(128), int64(0), int64(72)
	for h < len(rr) {
		m := int64(utf8.MaxRune)
		for _, r := range rr {
			if int64(r) >= n && int64(r) < m {
				m = int64(r)
			}
		}
		delta += (m - n) * int64(h+1)
		n = m
		for _, r := range rr {
			if int64(r) < n {
				delta++
			}
			if int64(r) == n {
				q := delta
				for k := int64(36); ; k += 36 {
					t := threshold(k, bias)
					if q < t {
						break
					}
					out = append(out, digit(t+(q-t)%(36-t)))
					q = (q - t) / (36 - t)
				}
				out = append(out, digit(q))
				bias = adapt(delta, int64(h+1), h == b)
				delta = 0
				h++
			}
		}
		delta++
		n++
	}
	return string(out)
}
func decodePuny(s string) ([]rune, error) {
	if len(s) == 0 || len(s) > 59 {
		return nil, errHost
	}
	out := []rune{}
	pos := 0
	if j := strings.LastIndexByte(s, '-'); j >= 0 {
		for _, r := range s[:j] {
			if r >= 128 {
				return nil, errHost
			}
			out = append(out, r)
		}
		pos = j + 1
	}
	n, i, bias := int64(128), int64(0), int64(72)
	for pos < len(s) {
		old := i
		w := int64(1)
		for k := int64(36); ; k += 36 {
			if pos >= len(s) {
				return nil, errHost
			}
			d := value(s[pos])
			pos++
			if d < 0 || w > 1<<40 || i+d*w > 1<<40 {
				return nil, errHost
			}
			i += d * w
			t := threshold(k, bias)
			if d < t {
				break
			}
			w *= 36 - t
		}
		size := int64(len(out) + 1)
		bias = adapt(i-old, size, old == 0)
		n += i / size
		i %= size
		if n > utf8.MaxRune || n < 128 || n >= 0xd800 && n <= 0xdfff {
			return nil, errHost
		}
		out = append(out, 0)
		copy(out[i+1:], out[i:])
		out[i] = rune(n)
		i++
	}
	return out, nil
}

// Restrict to selected scripts whose ordinary letters need no contextual or
// compatibility mapping. Unsupported scripts fail closed, not transliterated.
func supportedLetter(r rune) bool {
	return unicode.IsLetter(r) && (r >= 0xc0 && r <= 0xff || r >= 0x391 && r <= 0x3c9 || r >= 0x400 && r <= 0x4ff || r >= 0x3041 && r <= 0x3096 || r >= 0x30a1 && r <= 0x30fa || r >= 0x4e00 && r <= 0x9fff || r >= 0xac00 && r <= 0xd7a3)
}

// Refuse legacy numeric IPv4 spellings as ambiguous IP literals.
func ambiguousIP(labels []string) bool {
	for _, s := range labels {
		if s == "" {
			return false
		}
		base := 10
		if strings.HasPrefix(s, "0x") {
			base = 16
			s = s[2:]
			if s == "" {
				return false
			}
		}
		for _, r := range s {
			if !(r >= '0' && r <= '9' || base == 16 && r >= 'a' && r <= 'f') {
				return false
			}
		}
	}
	return true
}
