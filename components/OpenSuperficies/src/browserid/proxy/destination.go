package proxy

import (
	"context"
	"errors"
	"io"
	"net"
	"strconv"
	"sync"
	"time"
)

var errPrivate = errors.New("private_destination")

// Docker's default bridge (172.17/16) and reserved srf-idnet (172.30.80/24)
// are subsets of RFC1918. Treat CGNAT as private too, including mapped IPv4.
func privateIP(ip net.IP) bool {
	if ip == nil {
		return true
	}
	if ip.IsUnspecified() || ip.IsLoopback() || ip.IsPrivate() || ip.IsLinkLocalUnicast() || ip.IsLinkLocalMulticast() || ip.IsMulticast() {
		return true
	}
	v4 := ip.To4()
	return v4 != nil && v4[0] == 100 && v4[1] >= 64 && v4[1] <= 127
}
func (p *Proxy) vettedAddress(ctx context.Context, cap, host string, port int, v Verdict) (string, error) {
	ips := []net.IP{net.ParseIP(host)}
	if ips[0] == nil {
		var e error
		ips, e = p.c.Resolve(ctx, host)
		if e != nil || len(ips) == 0 {
			return "", errors.New("dns_unavailable")
		}
	}
	allowPrivate := false
	if pp, ok := p.c.Policy.(PrivateTargetPolicy); ok && v == Approved {
		allowPrivate = pp.AllowPrivate(cap, host)
	}
	// Reject the whole answer set if any candidate is forbidden. A private answer
	// must never be hidden behind a public first answer.
	for _, ip := range ips {
		if ip.To16() == nil {
			return "", errors.New("dns_unavailable")
		}
		if privateIP(ip) && !allowPrivate {
			return "", errPrivate
		}
	}
	return net.JoinHostPort(ips[0].String(), strconv.Itoa(port)), nil
}

type tunnelActivity struct {
	mu               sync.Mutex
	clock            func() time.Time
	idle             time.Duration
	end              time.Time
	client, upstream net.Conn
}

func (a *tunnelActivity) touch() {
	a.mu.Lock()
	defer a.mu.Unlock()
	deadline := a.clock().Add(a.idle)
	if deadline.After(a.end) {
		deadline = a.end
	}
	a.client.SetDeadline(deadline)
	a.upstream.SetDeadline(deadline)
}

type activeConn struct {
	net.Conn
	activity *tunnelActivity
}

func (c *activeConn) Read(b []byte) (int, error) {
	n, e := c.Conn.Read(b)
	if n > 0 {
		c.activity.touch()
	}
	return n, e
}
func (c *activeConn) Write(b []byte) (int, error) {
	n, e := c.Conn.Write(b)
	if n > 0 {
		c.activity.touch()
	}
	return n, e
}

// The hijacked reader may already contain buffered client input.
type activeReader struct {
	io.Reader
	activity *tunnelActivity
}

func (r *activeReader) Read(b []byte) (int, error) {
	n, e := r.Reader.Read(b)
	if n > 0 {
		r.activity.touch()
	}
	return n, e
}
