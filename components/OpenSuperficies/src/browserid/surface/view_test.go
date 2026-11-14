package surface

import (
	"bufio"
	"context"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestViewMountLifecycle(t *testing.T) {
	tool, _, launcher, _ := setup(t)
	c := start(t, tool)
	dir := filepath.Join(tool.Root, "view", c.Container)
	for path, mode := range map[string]os.FileMode{dir: 0700, filepath.Join(dir, "s"): 0777} {
		fi, err := os.Stat(path)
		if err != nil {
			t.Fatal(err)
		}
		if fi.Mode().Perm() != mode {
			t.Fatalf("%s: %o", path, fi.Mode().Perm())
		}
	}
	joined := strings.Join(launcher.plans[0].Argv, " ")
	if !strings.Contains(joined, "--mount type=bind,src="+filepath.Join(dir, "s")+",dst=/srfview") {
		t.Fatal(joined)
	}
	if strings.Contains(joined, "--publish") || strings.Contains(joined, "--no-sandbox") {
		t.Fatal(joined)
	}
	if err := tool.Stop(context.Background(), c.Container); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Lstat(dir); !os.IsNotExist(err) {
		t.Fatal("view directory retained", err)
	}
}

func fakeViewSocket(t *testing.T, h http.Handler) string {
	t.Helper()
	socket := filepath.Join(t.TempDir(), "xpra.sock")
	l, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	server := &http.Server{Handler: h, ReadHeaderTimeout: time.Second}
	done := make(chan struct{})
	go func() { defer close(done); _ = server.Serve(l) }()
	t.Cleanup(func() { _ = server.Close(); <-done })
	return socket
}

func TestViewHTTPAndAuthority(t *testing.T) {
	socket := fakeViewSocket(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Header.Get("Cookie") != "test-cookie" {
			t.Error("cookie lost")
		}
		w.Header().Set("X-Xpra-Test", "yes")
		_, _ = io.WriteString(w, r.URL.RequestURI())
	}))
	h, closeHandler := newViewHandler(context.Background(), socket, []byte("host jquery"), "127.0.0.1:18080")
	defer closeHandler()
	for _, tc := range []struct {
		host, origin, path string
		code               int
		body               string
	}{
		{"127.0.0.1:18080", "", "/index.html?test=ok", 200, "/index.html?test=ok"},
		{"127.0.0.1:18080", "http://127.0.0.1:18080", "/js/lib/jquery.js", 200, "host jquery"},
		{"evil.example", "", "/", 403, ""},
		{"127.0.0.1:18080", "http://evil.example", "/", 403, ""},
		{"127.0.0.1:18080", "http://127.0.0.1:18081", "/", 403, ""},
	} {
		r := httptest.NewRequest("GET", tc.path, nil)
		r.Host = tc.host
		r.RemoteAddr = "127.0.0.1:43210"
		if tc.origin != "" {
			r.Header.Set("Origin", tc.origin)
		}
		r.Header.Set("Cookie", "test-cookie")
		w := httptest.NewRecorder()
		h.ServeHTTP(w, r)
		if w.Code != tc.code || (tc.body != "" && w.Body.String() != tc.body) {
			t.Fatalf("%+v: %d %q", tc, w.Code, w.Body.String())
		}
	}
	r := httptest.NewRequest("GET", "/", nil)
	r.Host = "127.0.0.1:18080"
	r.RemoteAddr = "192.0.2.1:1234"
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	if w.Code != 403 {
		t.Fatal(w.Code)
	}
}

func TestViewWebsocketInterleaved(t *testing.T) {
	socket := fakeViewSocket(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, b, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer c.Close()
		_ = c.SetDeadline(time.Now().Add(3 * time.Second))
		_, _ = b.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n\x82\x01\xfe")
		_ = b.Flush()
		for i := 0; i < 32; i++ {
			frame := make([]byte, 3)
			if _, err = io.ReadFull(b, frame); err != nil {
				return
			}
			if _, err = c.Write(frame); err != nil {
				return
			}
		}
	}))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h, closeHandler := newViewHandler(ctx, socket, []byte("jquery"), "127.0.0.1:18080")
	defer closeHandler()
	s := httptest.NewServer(h)
	defer s.Close()
	c, err := net.DialTimeout("tcp", strings.TrimPrefix(s.URL, "http://"), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(3 * time.Second))
	// Include the first frame with the upgrade to exercise buffered bytes.
	_, err = io.WriteString(c, "GET / HTTP/1.1\r\nHost: 127.0.0.1:18080\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nOrigin: http://127.0.0.1:18080\r\n\r\n\x82\x01\x00")
	if err != nil {
		t.Fatal(err)
	}
	b := bufio.NewReader(c)
	resp, err := http.ReadResponse(b, nil)
	if err != nil || resp.StatusCode != 101 {
		t.Fatal(resp, err)
	}
	banner := make([]byte, 3)
	if _, err = io.ReadFull(b, banner); err != nil || string(banner) != "\x82\x01\xfe" {
		t.Fatal("server buffered frame", banner, err)
	}
	for i := 0; i < 32; i++ {
		frame := []byte{0x82, 1, byte(i)}
		if i > 0 {
			if _, err = c.Write(frame); err != nil {
				t.Fatal(err)
			}
		}
		got := make([]byte, 3)
		if _, err = io.ReadFull(b, got); err != nil || string(got) != string(frame) {
			t.Fatal(i, got, err)
		}
	}
}

func TestViewLoginRestore(t *testing.T) {
	for _, badRestore := range []bool{false, true} {
		t.Run(map[bool]string{false: "restore", true: "stop-on-failure"}[badRestore], func(t *testing.T) {
			tool, r, _, _ := setup(t)
			c := start(t, tool)
			modes := &viewModeRunner{base: r, failures: 0}
			if badRestore {
				modes.failures = 2
			}
			tool.R = modes
			tool.jqueryPath = filepath.Join(t.TempDir(), "jquery.js")
			if err := os.WriteFile(tool.jqueryPath, []byte("jquery"), 0600); err != nil {
				t.Fatal(err)
			}
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			err := tool.ViewAt(ctx, c.Container, 0, true, func(info ViewInfo) error {
				if info.Socket != tool.viewSocket(c.Container) || !strings.HasPrefix(info.URL, "http://127.0.0.1:") || info.Native != "xpra attach socket://"+info.Socket {
					t.Error(info)
				}
				cancel()
				return nil
			})
			if badRestore {
				if Code(err) != 6 {
					t.Fatal(err)
				}
			} else if err != nil {
				t.Fatal(err)
			}
			joined := ""
			for _, a := range r.calls {
				joined += strings.Join(a, " ") + "\n"
			}
			if !strings.Contains(joined, "SRF_LOGIN=1") || (!badRestore && !strings.Contains(joined, "SRF_LOGIN=0")) || (badRestore && !strings.Contains(joined, "xpra-stop")) {
				t.Fatal(joined)
			}
			if modes.restores != map[bool]int{false: 1, true: 2}[badRestore] {
				t.Fatal("restore attempts", modes.restores)
			}
			if strings.Contains(joined, "python3 -I -c") {
				t.Fatal("relay retained")
			}
		})
	}
}

func TestViewMissingJQueryAndNoLogin(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	tool.jqueryPath = filepath.Join(t.TempDir(), "absent")
	before := len(r.calls)
	if err := tool.ViewAt(context.Background(), c.Container, 0, true, func(ViewInfo) error { return nil }); Code(err) != 4 || !strings.Contains(err.Error(), "AUSENTE") {
		t.Fatal(err)
	}
	if len(r.calls) != before {
		t.Fatal("mode changed without jquery")
	}
	tool.jqueryPath = filepath.Join(t.TempDir(), "jquery.js")
	_ = os.WriteFile(tool.jqueryPath, []byte("jquery"), 0600)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	if err := tool.ViewAt(ctx, c.Container, 0, false, func(ViewInfo) error { cancel(); return nil }); err != nil {
		t.Fatal(err)
	}
	if len(r.calls) != before {
		t.Fatal("readonly view changed xpra mode")
	}
}

// Record mode changes even when the restart fails, without a stream port.
type viewModeRunner struct {
	base               *fakeRunner
	failures, restores int
}

func (r *viewModeRunner) Run(ctx context.Context, a []string, b []byte) ([]byte, error) {
	if strings.Contains(strings.Join(a, " "), "SRF_LOGIN=0") {
		r.restores++
		if r.restores <= r.failures {
			return nil, errors.New("fixture restore failure")
		}
	}
	return r.base.Run(ctx, a, b)
}

func TestViewRestoreRetries(t *testing.T) {
	_, base, _, _ := setup(t)
	modes := &viewModeRunner{base: base, failures: 1}
	tool := Tool{R: modes}
	if err := tool.restoreView("srf-id-test"); err != nil || modes.restores != 2 {
		t.Fatal(err, modes.restores)
	}
}

func TestViewCancelClosesUpgrade(t *testing.T) {
	closed := make(chan struct{})
	socket := fakeViewSocket(t, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		c, b, err := w.(http.Hijacker).Hijack()
		if err != nil {
			return
		}
		defer c.Close()
		defer close(closed)
		_ = c.SetDeadline(time.Now().Add(3 * time.Second))
		_, _ = b.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
		_ = b.Flush()
		_, _ = io.Copy(io.Discard, b)
	}))
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	h, cleanup := newViewHandler(ctx, socket, []byte("jquery"), "127.0.0.1:18080")
	defer cleanup()
	s := httptest.NewServer(h)
	defer s.Close()
	c, err := net.DialTimeout("tcp", strings.TrimPrefix(s.URL, "http://"), time.Second)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	_ = c.SetDeadline(time.Now().Add(2 * time.Second))
	_, _ = io.WriteString(c, "GET / HTTP/1.1\r\nHost: 127.0.0.1:18080\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
	if resp, err := http.ReadResponse(bufio.NewReader(c), nil); err != nil || resp.StatusCode != 101 {
		t.Fatal(resp, err)
	}
	cancel()
	select {
	case <-closed:
	case <-time.After(time.Second):
		t.Fatal("upstream upgrade survived session cancellation")
	}
	if _, err = c.Read(make([]byte, 1)); err == nil {
		t.Fatal("client upgrade survived cancellation")
	}
}

func TestViewBindAndAnnouncementFailure(t *testing.T) {
	for _, announceFail := range []bool{false, true} {
		t.Run(map[bool]string{false: "bind", true: "announce"}[announceFail], func(t *testing.T) {
			tool, base, _, _ := setup(t)
			c := start(t, tool)
			modes := &viewModeRunner{base: base}
			tool.R = modes
			tool.jqueryPath = filepath.Join(t.TempDir(), "jquery.js")
			_ = os.WriteFile(tool.jqueryPath, []byte("jquery"), 0600)
			if !announceFail {
				tool.Listen = func(context.Context, string, string) (net.Listener, error) { return nil, errors.New("fixture busy") }
			}
			before := len(base.calls)
			err := tool.ViewAt(context.Background(), c.Container, 0, true, func(ViewInfo) error { return errors.New("fixture announcement failure") })
			if announceFail {
				if Code(err) != 6 || modes.restores != 1 {
					t.Fatal(err, modes.restores)
				}
			} else if Code(err) != 4 || len(base.calls) != before {
				t.Fatal(err, base.calls[before:])
			}
		})
	}
}

func TestViewRejectsSymlinkParent(t *testing.T) {
	tool, _, _, _ := setup(t)
	if err := privateDir(tool.Root); err != nil {
		t.Fatal(err)
	}
	outside := t.TempDir()
	if err := os.Symlink(outside, filepath.Join(tool.Root, "view")); err != nil {
		t.Fatal(err)
	}
	if err := tool.createViewDir("srf-id-test"); Code(err) != 4 {
		t.Fatal(err)
	}
	entries, err := os.ReadDir(outside)
	if err != nil || len(entries) != 0 {
		t.Fatal("followed symlink", entries, err)
	}
}

func TestViewStopRetainsSocketUntilRemovalVerified(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	dir := filepath.Dir(filepath.Dir(tool.viewSocket(c.Container)))
	r.names = c.Container
	if err := tool.Stop(context.Background(), c.Container); Code(err) != 6 {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); err != nil {
		t.Fatal("cleaned before removal proof", err)
	}
	r.names = ""
	if err := tool.Stop(context.Background(), c.Container); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatal("view directory retained", err)
	}
}
