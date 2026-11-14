package proxy

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

type fakePolicy struct {
	verdict Verdict
	ports   bool
}

func (p fakePolicy) Decide(string, string) Verdict       { return p.verdict }
func (p fakePolicy) Account(string, string, int64) error { return nil }
func (p fakePolicy) AllowPort(string, string, int) bool  { return p.ports }
func harness(t *testing.T, p fakePolicy) (*Proxy, *bytes.Buffer, *int) {
	t.Helper()
	audit := new(bytes.Buffer)
	calls := new(int)
	x, e := New(Config{Resolve: publicResolver, Policy: p, Registry: func() (map[string]string, error) { return map[string]string{"cap": "192.0.2.2"}, nil }, Audit: audit, Clock: func() time.Time { return time.Unix(1, 0) }, Dial: func(context.Context, string, string) (net.Conn, error) {
		*calls++
		a, b := net.Pipe()
		go func() {
			defer b.Close()
			b.SetDeadline(time.Now().Add(3 * time.Second))
			req, e := http.ReadRequest(bufio.NewReader(b))
			if e == nil {
				io.Copy(io.Discard, req.Body)
				io.WriteString(b, "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
			}
		}()
		return a, nil
	}})
	if e != nil {
		t.Fatal(e)
	}
	return x, audit, calls
}
func TestVerdictsAndPrivacy(t *testing.T) {
	for _, v := range []Verdict{Approved, Pending, Denied} {
		t.Run(string(v), func(t *testing.T) {
			x, a, n := harness(t, fakePolicy{verdict: v})
			defer x.Close()
			r := httptest.NewRequest("GET", "http://Example.COM./private?secret=secret", nil)
			r.RemoteAddr = "192.0.2.2:1234"
			r.Header.Set("Cookie", "privatecookie")
			w := httptest.NewRecorder()
			x.ServeHTTP(w, r)
			if v != Denied {
				lines := strings.Split(strings.TrimSpace(a.String()), "\n")
				var rec auditRecord
				if len(lines) != 2 || json.Unmarshal([]byte(lines[1]), &rec) != nil || rec.Bytes <= 2 {
					t.Fatal("wire headers were not accounted", a.String())
				}
			}
			want := 200
			if v == Denied {
				want = 403
			}
			if w.Code != want {
				t.Fatalf("status %d body %s", w.Code, w.Body.String())
			}
			if (v == Denied && *n != 0) || (v != Denied && *n != 1) {
				t.Fatal("dial gate")
			}
			for _, s := range []string{"private", "secret", "Cookie", "privatecookie"} {
				if strings.Contains(a.String(), s) {
					t.Fatalf("audit leaks %q", s)
				}
			}
		})
	}
}
func TestNormalize(t *testing.T) {
	for _, tt := range []struct {
		in, want string
		bad      bool
	}{
		{"Example.COM.", "example.com", false}, {"bücher.example", "xn--bcher-kva.example", false}, {"127.0.0.1", "127.0.0.1", false}, {"user@example.com", "", true}, {"a..b", "", true}, {"a/b", "", true}, {"xn--", "", true}, {"127.1", "", true}, {"0x7f000001", "", true}, {"xn--bcher-kva.example", "xn--bcher-kva.example", false}, {"例え.テスト", "xn--r8jz45g.xn--zckzah", false}, {"example.com..", "", true}, {"ｅxample.com", "", true},
	} {
		got, e := NormalizeHost(tt.in)
		if (e != nil) != tt.bad || (!tt.bad && got != tt.want) {
			t.Errorf("%q -> %q %v", tt.in, got, e)
		}
	}
}
func TestIPAndConnectPort(t *testing.T) {
	for _, tt := range []struct {
		v           Verdict
		url, method string
		port        bool
		want        int
	}{
		{Pending, "http://127.0.0.1/", "GET", false, 403}, {Approved, "http://127.0.0.1/", "GET", false, 403},
		{Pending, "http://user@example.com/", "GET", false, 403}, {Approved, "example.com:8443", "CONNECT", false, 403},
		{Approved, "example.com:443", "CONNECT", false, 502}, {Approved, "example.com:8443", "CONNECT", true, 502},
	} {
		x, _, _ := harness(t, fakePolicy{tt.v, tt.port})
		r := httptest.NewRequest(tt.method, tt.url, nil)
		r.RemoteAddr = "192.0.2.2:123"
		w := httptest.NewRecorder()
		x.ServeHTTP(w, r)
		x.Close()
		if w.Code != tt.want {
			t.Errorf("%+v -> %d", tt, w.Code)
		}
	}
}
func TestQuotasAndClock(t *testing.T) {
	now := time.Unix(0, 0)
	q := newQuota(func() time.Time { return now })
	for i := range 20 {
		if e := q.admit("cap", string(rune('a'+i))); e != nil {
			t.Fatal(e)
		}
	}
	if q.admit("cap", "overflow") == nil {
		t.Fatal("new host passed")
	}
	if e := q.admit("cap", "a"); e != nil {
		t.Fatal("existing host cut")
	}
	if e := q.admit("other", "overflow"); e != nil {
		t.Fatal("other capsule cut")
	}
	if n := q.reserve("cap", "a", 50<<20); n != 50<<20 {
		t.Fatal(n)
	}
	if q.reserve("cap", "a", 1) != 0 {
		t.Fatal("byte overflow")
	}
	if q.reserve("cap", "b", 1) != 1 {
		t.Fatal("other host cut")
	}
	now = now.Add(time.Hour)
	if e := q.admit("cap", "overflow"); e != nil {
		t.Fatal(e)
	}
	if q.reserve("cap", "a", 1) != 1 {
		t.Fatal("did not expire")
	}
}

func TestByteLimitHTTPOnlyCutsHost(t *testing.T) {
	x, _, calls := harness(t, fakePolicy{verdict: Pending})
	defer x.Close()
	x.q.reserve("cap", "spent.example", PendingBytes)
	for _, tt := range []struct {
		host string
		want int
	}{{"spent.example", 403}, {"other.example", 200}} {
		r := httptest.NewRequest("GET", "http://"+tt.host+"/", nil)
		r.RemoteAddr = "192.0.2.2:4"
		w := httptest.NewRecorder()
		x.ServeHTTP(w, r)
		if w.Code != tt.want {
			t.Fatalf("%s = %d", tt.host, w.Code)
		}
	}
	if *calls != 1 {
		t.Fatal("exhausted host dialed")
	}
}

type hijackRecorder struct {
	*httptest.ResponseRecorder
	conn net.Conn
}

func (h hijackRecorder) Hijack() (net.Conn, *bufio.ReadWriter, error) {
	return h.conn, bufio.NewReadWriter(bufio.NewReader(h.conn), bufio.NewWriter(h.conn)), nil
}
func TestConnectTunnelAndShutdown(t *testing.T) {
	for _, port := range []string{"443", "8443"} {
		t.Run(port, func(t *testing.T) {
			audit := new(bytes.Buffer)
			x, e := New(Config{Resolve: publicResolver, Policy: fakePolicy{verdict: Pending, ports: true}, Registry: func() (map[string]string, error) { return map[string]string{"cap": "192.0.2.2"}, nil }, Audit: audit, Clock: time.Now, Dial: func(_ context.Context, _, address string) (net.Conn, error) {
				if address != "93.184.216.34:"+port {
					t.Error(address)
				}
				a, b := net.Pipe()
				go func() {
					defer b.Close()
					b.SetDeadline(time.Now().Add(2 * time.Second))
					v := make([]byte, 4)
					if _, e := io.ReadFull(b, v); e == nil {
						b.Write(v)
					}
				}()
				return a, nil
			}})
			if e != nil {
				t.Fatal(e)
			}
			defer x.Close()
			client, server := net.Pipe()
			defer client.Close()
			client.SetDeadline(time.Now().Add(2 * time.Second))
			r := httptest.NewRequest("CONNECT", "example.com:"+port, nil)
			r.RemoteAddr = "192.0.2.2:123"
			done := make(chan struct{})
			go func() { defer close(done); x.ServeHTTP(hijackRecorder{httptest.NewRecorder(), server}, r) }()
			br := bufio.NewReader(client)
			status, e := br.ReadString('\n')
			if e != nil || !strings.Contains(status, "200") {
				t.Fatalf("%q %v", status, e)
			}
			br.ReadString('\n')
			if _, e = client.Write([]byte("ping")); e != nil {
				t.Fatal(e)
			}
			b := make([]byte, 4)
			if _, e = io.ReadFull(br, b); e != nil || string(b) != "ping" {
				t.Fatalf("%q %v", b, e)
			}
			x.Close()
			select {
			case <-done:
			case <-time.After(2 * time.Second):
				t.Fatal("tunnel survived close")
			}
			if !strings.Contains(audit.String(), `"bytes":8`) {
				t.Fatal(audit.String())
			}
		})
	}
}
func TestUnknownCapsuleAndBadRegistry(t *testing.T) {
	for _, m := range []map[string]string{{"cap": "192.0.2.9"}, {"cap": "192.0.2.2", "duplicate": "192.0.2.2"}, {"bad\ncap": "192.0.2.2"}} {
		x, _, n := harness(t, fakePolicy{verdict: Approved})
		x.c.Registry = func() (map[string]string, error) { return m, nil }
		r := httptest.NewRequest("GET", "http://example.com/", nil)
		r.RemoteAddr = "192.0.2.2:1"
		w := httptest.NewRecorder()
		x.ServeHTTP(w, r)
		x.Close()
		if w.Code != 403 || *n != 0 {
			t.Fatal("registry failed open")
		}
	}
}

type brokenAudit struct{}

func (brokenAudit) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }
func TestAuditFailureStopsDial(t *testing.T) {
	x, _, n := harness(t, fakePolicy{verdict: Pending})
	defer x.Close()
	x.c.Audit = brokenAudit{}
	r := httptest.NewRequest("GET", "http://example.com/", nil)
	r.RemoteAddr = "192.0.2.2:1"
	w := httptest.NewRecorder()
	x.ServeHTTP(w, r)
	if w.Code != 503 || *n != 0 {
		t.Fatal("audit failed open")
	}
}
func TestMeteredConcurrentBudget(t *testing.T) {
	x, _, _ := harness(t, fakePolicy{verdict: Pending})
	defer x.Close()
	x.q.reserve("cap", "example.com", PendingBytes-7)
	var total atomic.Int64
	var wg sync.WaitGroup
	for range 8 {
		wg.Go(func() { m := metered{x, "cap", "example.com", Pending, io.Discard, &total}; m.Write([]byte("1234567")) })
	}
	wg.Wait()
	if total.Load() != 7 {
		t.Fatal(total.Load())
	}
}
