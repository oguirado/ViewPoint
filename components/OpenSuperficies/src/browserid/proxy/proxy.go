// Package proxy provides a forward proxy with capsule-scoped policy and quotas.
package proxy

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

type Verdict string

const (
	Approved       Verdict = "approved"
	Pending        Verdict = "pending"
	Denied         Verdict = "denied"
	RefusedPrivate Verdict = "refused-private"
)

// Policy is the integration seam for browserid/egress. Account receives actual
// wire byte counts in both directions, never HTTP metadata itself.
type Policy interface {
	Decide(capsule, host string) Verdict
	Account(capsule, host string, bytes int64) error
}

// PortPolicy explicitly permits non-443 CONNECT and non-80 HTTP targets.
// Without it those targets fail closed even when the hostname is approved.
type PortPolicy interface {
	AllowPort(capsule, host string, port int) bool
}

// PrivateTargetPolicy requires an operator-owned exact-host exception in addition
// to Approved. Implementations must default to false.
type PrivateTargetPolicy interface {
	AllowPrivate(capsule, host string) bool
}
type Config struct {
	Resolve           func(context.Context, string) ([]net.IP, error)
	MaxCapsuleTunnels int
	TunnelIdleTimeout time.Duration
	Policy            Policy
	Registry          func() (map[string]string, error)
	Audit             io.Writer
	Clock             func() time.Time
	Dial              func(context.Context, string, string) (net.Conn, error)
}
type Proxy struct {
	c       Config
	q       *quota
	auditMu sync.Mutex
	mu      sync.Mutex
	conns   map[net.Conn]bool
	closed  bool
	slots   chan struct{}
	tunnels map[string]int
}

func New(c Config) (*Proxy, error) {
	if c.Policy == nil || c.Registry == nil || c.Audit == nil || c.Clock == nil || c.Dial == nil {
		return nil, errors.New("missing proxy dependency")
	}
	if c.Resolve == nil {
		return nil, errors.New("missing resolver")
	}
	if c.MaxCapsuleTunnels == 0 {
		c.MaxCapsuleTunnels = 32
	}
	if c.TunnelIdleTimeout == 0 {
		c.TunnelIdleTimeout = time.Minute
	}
	if c.MaxCapsuleTunnels < 0 || c.TunnelIdleTimeout < 0 {
		return nil, errors.New("invalid tunnel limits")
	}
	return &Proxy{tunnels: map[string]int{}, c: c, q: newQuota(c.Clock), conns: map[net.Conn]bool{}, slots: make(chan struct{}, 256)}, nil
}

// Close also closes hijacked tunnels, which http.Server.Shutdown does not own.
func (p *Proxy) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.closed = true
	for c := range p.conns {
		c.Close()
	}
	return nil
}
func (p *Proxy) track(c net.Conn) bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		c.Close()
		return false
	}
	p.conns[c] = true
	return true
}
func (p *Proxy) forget(c net.Conn) { p.mu.Lock(); delete(p.conns, c); p.mu.Unlock(); c.Close() }

type auditRecord struct {
	Capsule string  `json:"capsule"`
	Host    string  `json:"host"`
	Port    int     `json:"port"`
	Verdict Verdict `json:"verdict"`
	Bytes   int64   `json:"bytes"`
	Reason  string  `json:"reason,omitempty"`
}

func (p *Proxy) audit(a auditRecord) error {
	p.auditMu.Lock()
	defer p.auditMu.Unlock()
	return json.NewEncoder(p.c.Audit).Encode(a)
}
func (p *Proxy) capsule(remote string) (string, error) {
	host, _, e := net.SplitHostPort(remote)
	if e != nil {
		return "", e
	}
	ip := net.ParseIP(host)
	if ip == nil {
		return "", errHost
	}
	m, e := p.c.Registry()
	if e != nil {
		return "", e
	}
	found := ""
	seen := map[string]bool{}
	for cap, address := range m {
		other := net.ParseIP(address)
		if !validCapsule(cap) || other == nil || seen[other.String()] {
			return "", errors.New("invalid capsule registry")
		}
		seen[other.String()] = true
		if other.Equal(ip) {
			found = cap
		}
	}
	if found == "" {
		return "", errors.New("unknown capsule")
	}
	return found, nil
}
func validCapsule(s string) bool {
	if len(s) == 0 || len(s) > 128 {
		return false
	}
	for _, r := range s {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '_' || r == '-' || r == '.') {
			return false
		}
	}
	return true
}
func target(r *http.Request) (string, int, error) {
	var u *url.URL
	if r.Method == http.MethodConnect {
		if strings.ContainsAny(r.Host, "/@?#\\") {
			return "", 0, errHost
		}
		h, port, e := net.SplitHostPort(r.Host)
		if e != nil {
			return "", 0, e
		}
		h, e = NormalizeHost(h)
		n, ne := strconv.Atoi(port)
		if e != nil || ne != nil || n < 1 || n > 65535 {
			return "", 0, errHost
		}
		return h, n, nil
	}
	u = r.URL
	if u.Scheme != "http" || u.Opaque != "" || u.User != nil || u.Host == "" || u.Fragment != "" {
		return "", 0, errHost
	}
	host, e := NormalizeHost(u.Hostname())
	if e != nil {
		return "", 0, e
	}
	port := 80
	if raw := u.Port(); raw != "" {
		port, e = strconv.Atoi(raw)
		if e != nil || port < 1 || port > 65535 {
			return "", 0, errHost
		}
	}
	return host, port, nil
}
func (p *Proxy) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	select {
	case p.slots <- struct{}{}:
		defer func() { <-p.slots }()
	default:
		http.Error(w, "proxy_busy", 503)
		return
	}
	p.mu.Lock()
	closed := p.closed
	p.mu.Unlock()
	if closed {
		http.Error(w, "stopping", 503)
		return
	}

	cap, e := p.capsule(r.RemoteAddr)
	if e != nil {
		http.Error(w, "unknown_capsule", 403)
		return
	}
	if r.Method == http.MethodConnect {
		p.mu.Lock()
		if p.tunnels[cap] >= p.c.MaxCapsuleTunnels {
			p.mu.Unlock()
			http.Error(w, "capsule_busy", 503)
			return
		}
		p.tunnels[cap]++
		p.mu.Unlock()
		defer func() {
			p.mu.Lock()
			p.tunnels[cap]--
			if p.tunnels[cap] == 0 {
				delete(p.tunnels, cap)
			}
			p.mu.Unlock()
		}()
	}
	host, port, e := target(r)
	if e != nil {
		http.Error(w, "invalid_target", 403)
		return
	}
	v := p.c.Policy.Decide(cap, host)
	rec := auditRecord{Capsule: cap, Host: host, Port: port, Verdict: v}
	refuse := func(reason string) {
		rec.Reason = reason
		if p.audit(rec) != nil {
			http.Error(w, "audit_unavailable", 503)
			return
		}
		http.Error(w, reason, 403)
	}
	if v != Approved && v != Pending {
		rec.Verdict = Denied
		refuse("policy_denied")
		return
	}
	if (r.Method == http.MethodConnect && port != 443) || (r.Method != http.MethodConnect && port != 80) {
		pp, ok := p.c.Policy.(PortPolicy)
		if !ok || !pp.AllowPort(cap, host, port) {
			if r.Method == http.MethodConnect {
				refuse("connect_port_denied")
			} else {
				refuse("http_port_denied")
			}
			return
		}
	}
	if ip := net.ParseIP(host); ip != nil && v != Approved {
		if privateIP(ip) {
			rec.Verdict = RefusedPrivate
			refuse("private_destination")
		} else {
			refuse("ip_requires_approval")
		}
		return
	}
	if v == Pending {
		if e := p.q.admit(cap, host); e != nil {
			if errors.Is(e, errLimit) {
				refuse("pending_byte_limit")
			} else {
				refuse("pending_host_limit")
			}
			return
		}
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	address, e := p.vettedAddress(ctx, cap, host, port, v)
	cancel()
	if errors.Is(e, errPrivate) {
		rec.Verdict = RefusedPrivate
		refuse("private_destination")
		return
	}
	if e != nil {
		rec.Reason = "dns_unavailable"
		if p.audit(rec) != nil {
			http.Error(w, "audit_unavailable", 503)
		} else {
			http.Error(w, "upstream_unavailable", 502)
		}
		return
	}

	if p.audit(rec) != nil {
		http.Error(w, "audit_unavailable", 503)
		return
	}
	var transferred atomic.Int64
	defer func() {
		rec.Bytes = transferred.Load()
		if p.audit(rec) != nil {
			p.Close()
		}
	}()
	if r.Method == http.MethodConnect {
		p.connect(w, r, cap, host, address, v, &transferred)
		return
	}
	p.forward(w, r, cap, host, port, address, v, &transferred)
}

var errLimit = errors.New("pending_byte_limit")

type metered struct {
	p         *Proxy
	cap, host string
	v         Verdict
	dst       io.Writer
	total     *atomic.Int64
}

func (m metered) Write(b []byte) (int, error) {
	size := len(b)
	var at time.Time
	reserved := int64(size)
	if m.v == Pending {
		reserved, at = m.p.q.reserveAt(m.cap, m.host, int64(size))
		b = b[:int(reserved)]
		if len(b) == 0 {
			return 0, errLimit
		}
	}
	n, e := m.dst.Write(b)
	if m.v == Pending {
		m.p.q.refund(m.cap, m.host, at, reserved-int64(n))
	}
	m.total.Add(int64(n))
	if n > 0 {
		if ae := m.p.c.Policy.Account(m.cap, m.host, int64(n)); ae != nil {
			return n, ae
		}
	}
	if e == nil && n < size {
		return n, errLimit
	}
	return n, e
}
func (p *Proxy) connect(w http.ResponseWriter, r *http.Request, cap, host, address string, v Verdict, total *atomic.Int64) {
	h, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "tunnel_unavailable", 502)
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	up, e := p.c.Dial(ctx, "tcp", address)
	cancel()
	if e != nil {
		http.Error(w, "upstream_unavailable", 502)
		return
	}
	if !p.track(up) {
		http.Error(w, "stopping", 503)
		return
	}
	defer p.forget(up)
	client, rw, e := h.Hijack()
	if client != nil {
		defer client.Close()
	}
	if e != nil {
		return
	}
	if !p.track(client) {
		return
	}
	defer p.forget(client)
	// Refresh both legs after activity, capped by an absolute lifetime.
	activity := &tunnelActivity{clock: p.c.Clock, idle: p.c.TunnelIdleTimeout, end: p.c.Clock().Add(time.Hour), client: client, upstream: up}
	activity.touch()
	activeUp := &activeConn{Conn: up, activity: activity}
	activeClient := &activeConn{Conn: client, activity: activity}
	if _, e = rw.WriteString("HTTP/1.1 200 Connection Established\r\n\r\n"); e != nil {
		return
	}
	if e = rw.Flush(); e != nil {
		return
	}
	done := make(chan struct{})
	go func() {
		defer close(done)
		io.Copy(metered{p, cap, host, v, activeUp, total}, &activeReader{Reader: rw, activity: activity})
		up.Close()
		client.Close()
	}()
	io.Copy(metered{p, cap, host, v, activeClient, total}, activeUp)
	up.Close()
	client.Close()
	<-done
}
func stripHop(h http.Header) {
	for _, v := range h.Values("Connection") {
		for _, s := range strings.Split(v, ",") {
			h.Del(strings.TrimSpace(s))
		}
	}
	for _, s := range []string{"Connection", "Proxy-Connection", "Proxy-Authorization", "Proxy-Authenticate", "Keep-Alive", "TE", "Trailer", "Transfer-Encoding", "Upgrade"} {
		h.Del(s)
	}
}
func (p *Proxy) forward(w http.ResponseWriter, r *http.Request, cap, host string, port int, address string, v Verdict, total *atomic.Int64) {
	ctx, cancel := context.WithTimeout(r.Context(), time.Minute)
	defer cancel()
	out := r.Clone(ctx)
	out.RequestURI = ""
	out.URL.Host = net.JoinHostPort(host, strconv.Itoa(port))
	out.Host = out.URL.Host
	out.Header = r.Header.Clone()
	stripHop(out.Header)
	// No environment proxy, no cross-request pooling, no redirect handling.
	tr := &http.Transport{Proxy: nil, DisableKeepAlives: true, ResponseHeaderTimeout: 20 * time.Second, MaxResponseHeaderBytes: 64 << 10, DialContext: func(ctx context.Context, network, _ string) (net.Conn, error) {
		c, e := p.c.Dial(ctx, network, address)
		if e != nil {
			return nil, e
		}
		if !p.track(c) {
			return nil, errors.New("stopping")
		}
		return &wireConn{Conn: c, m: metered{p, cap, host, v, c, total}}, nil
	}}
	defer tr.CloseIdleConnections()
	resp, e := tr.RoundTrip(out)
	if e != nil {
		http.Error(w, "upstream_unavailable", 502)
		return
	}
	defer resp.Body.Close()
	stripHop(resp.Header)
	for k, values := range resp.Header {
		for _, value := range values {
			w.Header().Add(k, value)
		}
	}
	w.WriteHeader(resp.StatusCode)
	if _, e = io.Copy(w, resp.Body); e != nil { // HTTP/1 must terminate a truncated body, including chunked responses.
		panic(http.ErrAbortHandler)
	}
}

// wireConn accounts HTTP request/response lines and headers as byte counts,
// so header-only traffic cannot evade the pending host budget.
type wireConn struct {
	net.Conn
	m metered
}

func (c *wireConn) Write(b []byte) (int, error) { return c.m.Write(b) }
func (c *wireConn) Read(b []byte) (int, error) {
	var at time.Time
	reserved := int64(len(b))
	if c.m.v == Pending {
		reserved, at = c.m.p.q.reserveAt(c.m.cap, c.m.host, reserved)
		if reserved == 0 {
			return 0, errLimit
		}
		b = b[:int(reserved)]
	}
	n, e := c.Conn.Read(b)
	if c.m.v == Pending {
		c.m.p.q.refund(c.m.cap, c.m.host, at, reserved-int64(n))
	}
	c.m.total.Add(int64(n))
	if n > 0 {
		if ae := c.m.p.c.Policy.Account(c.m.cap, c.m.host, int64(n)); ae != nil {
			return n, ae
		}
	}
	return n, e
}
func (c *wireConn) Close() error { c.m.p.forget(c.Conn); return nil }

var _ http.Handler = (*Proxy)(nil)
