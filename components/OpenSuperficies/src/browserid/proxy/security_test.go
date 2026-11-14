package proxy

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http/httptest"
	"reflect"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// Reflection lets these regression tests execute against the old Config too.
func configField(t *testing.T, x *Proxy, name string, value any) {
	t.Helper()
	f := reflect.ValueOf(&x.c).Elem().FieldByName(name)
	if !f.IsValid() {
		t.Fatalf("missing security dependency %s", name)
	}
	f.Set(reflect.ValueOf(value))
}
func request(x *Proxy, method, target string) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, target, nil)
	r.RemoteAddr = "192.0.2.2:1"
	w := httptest.NewRecorder()
	if method == "CONNECT" {
		x.ServeHTTP(errorHijacker{w, nil}, r)
	} else {
		x.ServeHTTP(w, r)
	}
	return w
}
func TestPrivateLiteralNeedsSeparatePermission(t *testing.T) {
	x, a, n := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	w := request(x, "GET", "http://127.0.0.1/private?secret=yes")
	if w.Code != 403 || *n != 0 || !strings.Contains(a.String(), `"verdict":"refused-private"`) {
		t.Fatalf("private literal escaped: %d %d %s", w.Code, *n, a.String())
	}
}
func TestResolvedPrivateBothProtocols(t *testing.T) {
	for _, ip := range []string{"127.0.0.1", "10.1.2.3", "172.17.0.1", "172.30.80.2", "192.168.2.1", "100.64.0.1", "169.254.169.254", "0.0.0.0", "224.0.0.1", "::1", "::", "fe80::1", "fc00::1", "ff02::1", "::ffff:127.0.0.1"} {
		for _, method := range []string{"GET", "CONNECT"} {
			t.Run(ip+method, func(t *testing.T) {
				x, a, n := harness(t, fakePolicy{verdict: Approved})
				defer x.Close()
				configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) {
					return []net.IP{net.ParseIP("93.184.216.34"), net.ParseIP(ip)}, nil
				})
				target := "http://rebind.example/secret?private=yes"
				if method == "CONNECT" {
					target = "rebind.example:443"
				}
				w := request(x, method, target)
				if w.Code != 403 || *n != 0 || !strings.Contains(a.String(), `"verdict":"refused-private"`) || strings.Contains(a.String(), "secret") {
					t.Fatalf("escaped %d %d %s", w.Code, *n, a.String())
				}
			})
		}
	}
}
func TestVettedIPDialAndResolverFailure(t *testing.T) {
	for _, method := range []string{"GET", "CONNECT"} {
		x, _, _ := harness(t, fakePolicy{verdict: Approved})
		defer x.Close()
		resolves := 0
		address := ""
		configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) {
			resolves++
			if resolves > 1 {
				return []net.IP{net.ParseIP("127.0.0.1")}, nil
			}
			return []net.IP{net.ParseIP("93.184.216.34")}, nil
		})
		x.c.Dial = func(_ context.Context, _, a string) (net.Conn, error) { address = a; return nil, io.ErrClosedPipe }
		target := "http://rebind.example/"
		want := "93.184.216.34:80"
		if method == "CONNECT" {
			target = "rebind.example:443"
			want = "93.184.216.34:443"
		}
		request(x, method, target)
		if resolves != 1 || address != want {
			t.Fatalf("DNS rebound: %d %s", resolves, address)
		}
		for _, ips := range [][]net.IP{nil, {nil}} {
			configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) { return ips, nil })
			address = ""
			request(x, method, target)
			if address != "" {
				t.Fatal("empty/invalid DNS dialed")
			}
		}
		configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) { return nil, errors.New("private DNS detail") })
		address = ""
		request(x, method, target)
		if address != "" {
			t.Fatal("failed DNS dialed")
		}
	}
}
func TestExhaustedHostDoesNotConsumeAdmission(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Pending})
	defer x.Close()
	x.q.reserve("cap", "spent.example", PendingBytes)
	request(x, "GET", "http://spent.example/")
	if _, ok := x.q.hosts["cap"]["spent.example"]; ok {
		t.Fatal("exhausted host consumed new-host slot")
	}
}

type errorHijacker struct {
	*httptest.ResponseRecorder
	conn net.Conn
}

func (h errorHijacker) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return h.conn, nil, io.ErrClosedPipe
}
func TestHijackErrorClosesReturnedSocket(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	up, peer := net.Pipe()
	defer peer.Close()
	x.c.Dial = func(context.Context, string, string) (net.Conn, error) { return up, nil }
	c, s := net.Pipe()
	defer c.Close()
	defer s.Close()
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.2:1"
	x.ServeHTTP(errorHijacker{httptest.NewRecorder(), s}, r)
	c.SetReadDeadline(time.Now().Add(100 * time.Millisecond))
	_, e := c.Read(make([]byte, 1))
	if e != io.EOF {
		t.Fatalf("hijacked socket not closed: %v", e)
	}
}
func TestCapsuleTunnelCap(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	configField(t, x, "MaxCapsuleTunnels", 1)
	entered := make(chan struct{})
	release := make(chan struct{})
	var once sync.Once
	defer once.Do(func() { close(release) })
	var calls atomic.Int32
	x.c.Registry = func() (map[string]string, error) {
		return map[string]string{"cap": "192.0.2.2", "other": "192.0.2.3"}, nil
	}
	x.c.Dial = func(ctx context.Context, _, _ string) (net.Conn, error) {
		if calls.Add(1) > 1 {
			return nil, io.ErrClosedPipe
		}
		close(entered)
		select {
		case <-release:
		case <-ctx.Done():
		}
		return nil, io.ErrClosedPipe
	}
	done := make(chan struct{})
	go func() { defer close(done); request(x, "CONNECT", "example.com:443") }()
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("first tunnel did not dial")
	}
	// A second attempt must be refused before dialing, without taking another capsule's slot.
	w := request(x, "CONNECT", "example.com:443")
	if w.Code != 503 {
		t.Fatalf("capsule cap %d", w.Code)
	}
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.3:1"
	other := httptest.NewRecorder()
	x.ServeHTTP(errorHijacker{other, nil}, r)
	if other.Code != 502 || calls.Load() != 2 {
		t.Fatal("other capsule starved")
	}
	once.Do(func() { close(release) })
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("tunnel did not exit")
	}
	if request(x, "CONNECT", "example.com:443").Code != 502 || calls.Load() != 3 {
		t.Fatal("slot not released")
	}
}

func publicResolver(context.Context, string) ([]net.IP, error) {
	return []net.IP{net.ParseIP("93.184.216.34")}, nil
}

type privatePolicy struct {
	fakePolicy
	permit bool
}

func (p privatePolicy) AllowPrivate(_, host string) bool {
	return p.permit && host == "private.example"
}
func TestPrivateExceptionRequiresBothExactApprovalAndPermission(t *testing.T) {
	for _, v := range []Verdict{Approved, Pending, Denied} {
		for _, permit := range []bool{false, true} {
			for _, method := range []string{"GET", "CONNECT"} {
				x, _, n := harness(t, fakePolicy{verdict: v})
				x.c.Policy = privatePolicy{fakePolicy{verdict: v}, permit}
				configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) { return []net.IP{net.ParseIP("10.1.2.3")}, nil })
				target := "http://private.example/"
				if method == "CONNECT" {
					target = "private.example:443"
				}
				request(x, method, target)
				want := 0
				if v == Approved && permit {
					want = 1
				}
				if *n != want {
					t.Fatalf("%s permit=%t %s dials=%d", v, permit, method, *n)
				}
				target = "http://different.example/"
				if method == "CONNECT" {
					target = "different.example:443"
				}
				request(x, method, target)
				if *n != want {
					t.Fatal("exception matched wrong host")
				}
				x.Close()
			}
		}
	}
}
func TestHTTPNonstandardPortNeedsPermission(t *testing.T) {
	for _, allow := range []bool{false, true} {
		x, _, n := harness(t, fakePolicy{verdict: Approved, ports: allow})
		w := request(x, "GET", "http://example.com:8080/")
		x.Close()
		if (allow && (w.Code != 200 || *n != 1)) || (!allow && (w.Code != 403 || *n != 0)) {
			t.Fatal("HTTP port gate", w.Code, *n)
		}
	}
}

type closingHijacker struct {
	hijackRecorder
	stop func()
}

func (h closingHijacker) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	h.stop()
	return h.hijackRecorder.Hijack()
}
func TestHijackShutdownRaceClosesSocket(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	up, peer := net.Pipe()
	defer peer.Close()
	x.c.Dial = func(context.Context, string, string) (net.Conn, error) { return up, nil }
	c, s := net.Pipe()
	defer c.Close()
	defer s.Close()
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.2:1"
	x.ServeHTTP(closingHijacker{hijackRecorder{httptest.NewRecorder(), s}, func() { x.Close() }}, r)
	c.SetReadDeadline(time.Now().Add(time.Second))
	_, e := c.Read(make([]byte, 1))
	if e != io.EOF {
		t.Fatalf("shutdown socket survived: %v", e)
	}
}

// Deadline recording uses an injected clock: no sleep and no external socket.
type deadlineConn struct {
	net.Conn
	mu       sync.Mutex
	deadline time.Time
}

func (c *deadlineConn) SetDeadline(at time.Time) error {
	c.mu.Lock()
	c.deadline = at
	c.mu.Unlock()
	return nil
}
func (c *deadlineConn) last() time.Time { c.mu.Lock(); defer c.mu.Unlock(); return c.deadline }
func TestTunnelIdleDeadlinesRefreshAndRemainBounded(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	configField(t, x, "TunnelIdleTimeout", time.Minute)
	// Future wall time keeps the underlying test pipe's safety deadline separate
	// from the fake proxy clock; recorded deadlines are asserted exactly.
	base := time.Date(2040, 1, 1, 0, 0, 0, 0, time.UTC)
	var tick atomic.Int64
	x.c.Clock = func() time.Time { return base.Add(time.Duration(tick.Load())) }
	a, b := net.Pipe()
	defer b.Close()
	up := &deadlineConn{Conn: a}
	c, s := net.Pipe()
	defer c.Close()
	client := &deadlineConn{Conn: s}
	x.c.Dial = func(context.Context, string, string) (net.Conn, error) { return up, nil }
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.2:1"
	done := make(chan struct{})
	go func() { defer close(done); x.ServeHTTP(hijackRecorder{httptest.NewRecorder(), client}, r) }()
	c.SetDeadline(time.Now().Add(2 * time.Second))
	b.SetDeadline(time.Now().Add(2 * time.Second))
	br := bufio.NewReader(c)
	if _, e := br.ReadString('\n'); e != nil {
		t.Fatal(e)
	}
	br.ReadString('\n')
	for _, elapsed := range []time.Duration{0, 30 * time.Second, 59*time.Minute + 30*time.Second} {
		tick.Store(int64(elapsed))
		writeDone := make(chan error, 1)
		go func() { _, e := c.Write([]byte("x")); writeDone <- e }()
		if _, e := io.ReadFull(b, make([]byte, 1)); e != nil {
			t.Fatal(e)
		}
		if e := <-writeDone; e != nil {
			t.Fatal(e)
		}
		// Echo forces both copy directions through the activity wrapper.
		echoDone := make(chan error, 1)
		go func() { _, e := b.Write([]byte("x")); echoDone <- e }()
		if _, e := io.ReadFull(br, make([]byte, 1)); e != nil {
			t.Fatal(e)
		}
		if e := <-echoDone; e != nil {
			t.Fatal(e)
		}
		want := base.Add(min(elapsed+time.Minute, time.Hour))
		if !up.last().Equal(want) || !client.last().Equal(want) {
			t.Fatalf("deadline %v %v want %v", up.last(), client.last(), want)
		}
	}
	x.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("tunnel survived shutdown")
	}
}
func TestIdleTunnelClosesBothLegs(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	x.c.Clock = time.Now
	configField(t, x, "TunnelIdleTimeout", 20*time.Millisecond)
	a, b := net.Pipe()
	defer b.Close()
	c, s := net.Pipe()
	defer c.Close()
	x.c.Dial = func(context.Context, string, string) (net.Conn, error) { return a, nil }
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.2:1"
	done := make(chan struct{})
	go func() { defer close(done); x.ServeHTTP(hijackRecorder{httptest.NewRecorder(), s}, r) }()
	c.SetDeadline(time.Now().Add(time.Second))
	br := bufio.NewReader(c)
	br.ReadString('\n')
	br.ReadString('\n')
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("idle tunnel survived")
	}
	if _, e := c.Read(make([]byte, 1)); e != io.EOF {
		t.Fatal("client leg", e)
	}
	b.SetReadDeadline(time.Now().Add(time.Second))
	if _, e := b.Read(make([]byte, 1)); e != io.EOF {
		t.Fatal("upstream leg", e)
	}
	x.mu.Lock()
	defer x.mu.Unlock()
	if len(x.tunnels) != 0 || len(x.conns) != 0 {
		t.Fatal("idle tunnel leaked tracking")
	}
}

func TestQuotaRefusalDoesNotResolveNewNames(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Pending})
	defer x.Close()
	for i := range PendingHosts {
		if e := x.q.admit("cap", string(rune('a'+i))+".example"); e != nil {
			t.Fatal(e)
		}
	}
	resolves := 0
	configField(t, x, "Resolve", func(context.Context, string) ([]net.IP, error) {
		resolves++
		return publicResolver(context.Background(), "")
	})
	w := request(x, "GET", "http://new.example/")
	if w.Code != 403 || resolves != 0 {
		t.Fatalf("quota-refused name reached DNS: status=%d resolves=%d", w.Code, resolves)
	}
}

type notifyWriteConn struct {
	net.Conn
	entered chan struct{}
}

func (c *notifyWriteConn) Write(b []byte) (int, error) { close(c.entered); return c.Conn.Write(b) }
func TestClientActivityRefreshesBeforeBlockedForward(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Approved})
	defer x.Close()
	base := time.Date(2040, 1, 1, 0, 0, 0, 0, time.UTC)
	var elapsed atomic.Int64
	x.c.Clock = func() time.Time { return base.Add(time.Duration(elapsed.Load())) }
	a, b := net.Pipe()
	defer b.Close()
	entered := make(chan struct{})
	up := &deadlineConn{Conn: &notifyWriteConn{Conn: a, entered: entered}}
	c, s := net.Pipe()
	defer c.Close()
	client := &deadlineConn{Conn: s}
	x.c.Dial = func(context.Context, string, string) (net.Conn, error) { return up, nil }
	r := httptest.NewRequest("CONNECT", "example.com:443", nil)
	r.RemoteAddr = "192.0.2.2:1"
	done := make(chan struct{})
	go func() { defer close(done); x.ServeHTTP(hijackRecorder{httptest.NewRecorder(), client}, r) }()
	c.SetDeadline(time.Now().Add(time.Second))
	br := bufio.NewReader(c)
	br.ReadString('\n')
	br.ReadString('\n')
	elapsed.Store(int64(30 * time.Second))
	if _, e := c.Write([]byte("x")); e != nil {
		t.Fatal(e)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("forward did not start")
	}
	if want := base.Add(90 * time.Second); !up.last().Equal(want) || !client.last().Equal(want) {
		t.Errorf("client activity did not refresh both deadlines: %v %v", up.last(), client.last())
	}
	x.Close()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("blocked forward survived shutdown")
	}
}
