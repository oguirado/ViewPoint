package surface

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"io"
	"log"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

const hostJQuery = "/usr/share/javascript/jquery/jquery.js"

type ViewInfo struct {
	URL    string `json:"url"`
	Native string `json:"native"`
	Socket string `json:"socket"`
}

func (t Tool) viewSocket(c string) string {
	return filepath.Join(t.root(), "view", c, "s", "xpra.sock")
}

func (t Tool) createViewDir(c string) error {
	parent := filepath.Dir(filepath.Dir(t.viewSocket(c)))
	if err := privateDir(filepath.Dir(parent)); err != nil {
		return err
	}
	if err := privateDir(parent); err != nil {
		return err
	}
	inner := filepath.Join(parent, "s")
	// Mkdir must create a fresh directory, never chmod a preexisting symlink.
	if err := os.Mkdir(inner, 0700); err != nil {
		return fail(4, "view socket directory already exists or unavailable")
	}
	if err := os.Chmod(inner, 0777); err != nil {
		return fail(4, "view socket directory permissions unavailable")
	}
	return nil
}

// newViewHandler keeps all traffic in Go; upgrades preserve buffered bytes.
func newViewHandler(ctx context.Context, socket string, jquery []byte, authority string) (http.Handler, func()) {
	ctx, cancel := context.WithCancel(ctx)
	var mu sync.Mutex
	var handlers sync.WaitGroup
	stopped := false
	transport := &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{Timeout: 5 * time.Second}).DialContext(ctx, "unix", socket)
		},
		MaxIdleConns: 16, MaxIdleConnsPerHost: 16, MaxConnsPerHost: 32,
		IdleConnTimeout: 30 * time.Second, ResponseHeaderTimeout: 10 * time.Second,
	}
	proxy := &httputil.ReverseProxy{
		Rewrite: func(p *httputil.ProxyRequest) {
			p.SetURL(&url.URL{Scheme: "http", Host: "xpra"})
			// Keep the browser's loopback authority for xpra's websocket checks.
			p.Out.Host = authority
		},
		Transport:     transport,
		FlushInterval: -1,
		ErrorLog:      log.New(io.Discard, "", 0),
		ErrorHandler: func(w http.ResponseWriter, _ *http.Request, _ error) {
			http.Error(w, "xpra socket unavailable", http.StatusBadGateway)
		},
	}
	handler := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		mu.Lock()
		if stopped {
			mu.Unlock()
			http.Error(w, "viewer stopped", http.StatusServiceUnavailable)
			return
		}
		handlers.Add(1)
		mu.Unlock()
		defer handlers.Done()
		remote, _, err := net.SplitHostPort(r.RemoteAddr)
		ip := net.ParseIP(remote)
		if err != nil || ip == nil || !ip.IsLoopback() || r.Host != authority {
			http.Error(w, "loopback authority required", http.StatusForbidden)
			return
		}
		if origins := r.Header.Values("Origin"); len(origins) > 0 {
			if len(origins) != 1 || origins[0] != "http://"+authority {
				http.Error(w, "loopback origin required", http.StatusForbidden)
				return
			}
		}
		if r.URL.Path == "/js/lib/jquery.js" {
			if r.Method != "GET" && r.Method != "HEAD" {
				w.WriteHeader(http.StatusMethodNotAllowed)
				return
			}
			w.Header().Set("Content-Type", "application/javascript")
			http.ServeContent(w, r, "jquery.js", time.Time{}, bytes.NewReader(jquery))
			return
		}
		if strings.EqualFold(r.Header.Get("Upgrade"), "websocket") {
			spliceView(ctx, w, r, socket)
			return
		}
		// A session context also bounds active HTTP requests.
		reqCtx, cancel := context.WithCancel(r.Context())
		defer cancel()
		stop := context.AfterFunc(ctx, cancel)
		defer stop()
		proxy.ServeHTTP(w, r.WithContext(reqCtx))
	})
	return handler, func() {
		mu.Lock()
		stopped = true
		mu.Unlock()
		cancel()
		handlers.Wait()
		transport.CloseIdleConnections()
	}
}

func headerHasToken(value, token string) bool {
	for _, part := range strings.Split(value, ",") {
		if strings.EqualFold(strings.TrimSpace(part), token) {
			return true
		}
	}
	return false
}

// spliceView forwards opaque websocket bytes without parsing or logging frames.
// Read from both buffered readers, since either handshake can include data.
func spliceView(ctx context.Context, w http.ResponseWriter, r *http.Request, socket string) {
	if r.Method != "GET" || r.ContentLength != 0 || len(r.TransferEncoding) > 0 || !headerHasToken(r.Header.Get("Connection"), "upgrade") {
		http.Error(w, "invalid websocket request", http.StatusBadRequest)
		return
	}
	upstream, err := (&net.Dialer{Timeout: 5 * time.Second}).DialContext(r.Context(), "unix", socket)
	if err != nil {
		http.Error(w, "xpra socket unavailable", http.StatusBadGateway)
		return
	}
	defer upstream.Close()
	stopUpstream := context.AfterFunc(ctx, func() { _ = upstream.Close() })
	defer stopUpstream()
	stopRequest := context.AfterFunc(r.Context(), func() { _ = upstream.Close() })
	defer stopRequest()
	_ = upstream.SetDeadline(time.Now().Add(10 * time.Second))
	req := r.Clone(r.Context())
	req.RequestURI = ""
	req.URL.Scheme = ""
	req.URL.Host = ""
	if err = req.Write(upstream); err != nil {
		http.Error(w, "xpra upgrade unavailable", http.StatusBadGateway)
		return
	}
	reader := bufio.NewReader(upstream)
	response, err := http.ReadResponse(reader, req)
	if err != nil {
		http.Error(w, "xpra upgrade unavailable", http.StatusBadGateway)
		return
	}
	if response.StatusCode != http.StatusSwitchingProtocols || !strings.EqualFold(response.Header.Get("Upgrade"), "websocket") || !headerHasToken(response.Header.Get("Connection"), "upgrade") {
		_ = response.Body.Close()
		http.Error(w, "xpra upgrade refused", http.StatusBadGateway)
		return
	}
	client, buffer, err := http.NewResponseController(w).Hijack()
	if err != nil {
		return
	}
	defer client.Close()
	stopClient := context.AfterFunc(ctx, func() { _ = client.Close() })
	defer stopClient()
	deadline := time.Now().Add(30 * time.Minute)
	_ = upstream.SetDeadline(deadline)
	_ = client.SetDeadline(deadline)
	response.Body = nil
	if err = response.Write(buffer); err != nil {
		return
	}
	if err = buffer.Flush(); err != nil {
		return
	}
	done := make(chan struct{}, 2)
	go func() { _, _ = io.Copy(upstream, buffer.Reader); done <- struct{}{} }()
	go func() { _, _ = io.Copy(client, reader); done <- struct{}{} }()
	<-done
	_ = upstream.Close()
	_ = client.Close()
	<-done
}

func (t Tool) restoreView(c string) error {
	for attempt := 0; attempt < 2; attempt++ {
		ctx, cancel := context.WithTimeout(context.Background(), 130*time.Second)
		err := t.xpra(ctx, c, false)
		cancel()
		if err == nil {
			return nil
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	_, err := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001:10001", c, "timeout", "--signal=TERM", "--kill-after=1s", "15s", "/usr/local/bin/identity-entrypoint.sh", "xpra-stop"}, nil)
	if err != nil {
		return fail(6, "readonly restore and viewer stop failed")
	}
	return fail(6, "readonly restore failed; viewer stopped")
}

func (t Tool) ViewAt(ctx context.Context, c string, port int, login bool, announce func(ViewInfo) error) (err error) {
	return t.viewAt(ctx, c, port, login, "", announce)
}

// ViewRecord is an operator login view that records a demonstration into RAM (record.go).
func (t Tool) ViewRecord(ctx context.Context, c string, port int, demo string, announce func(ViewInfo) error) error {
	if t.S == nil {
		return fail(3, "streamer required for recording")
	}
	if !demoRE.MatchString(demo) {
		return fail(2, "demo id must be 1-40 chars [a-z0-9-]")
	}
	return t.viewAt(ctx, c, port, true, demo, announce)
}

func (t Tool) viewAt(ctx context.Context, c string, port int, login bool, demo string, announce func(ViewInfo) error) (err error) {
	if !capsuleRE.MatchString(c) || port < 0 || port > 65535 {
		return fail(2, "invalid identity capsule or loopback port")
	}
	if announce == nil || t.R == nil {
		return fail(3, "runner and view callback required")
	}
	ctx, end := context.WithTimeout(ctx, 24*time.Hour)
	defer end()
	unlock, err := t.lock(ctx)
	if err != nil {
		return err
	}
	defer func() {
		if unlock != nil {
			unlock()
		}
	}()
	s, err := t.load(c)
	if err != nil {
		return err
	}
	if !s.Ready {
		return fail(4, "capsule not ready")
	}
	jqueryPath := t.jqueryPath
	if jqueryPath == "" {
		jqueryPath = hostJQuery
	}
	jquery, err := os.ReadFile(jqueryPath)
	if err != nil {
		return fail(4, "AUSENTE — host jquery unavailable")
	}
	listen := t.Listen
	if listen == nil {
		listen = (&net.ListenConfig{}).Listen
	}
	listener, err := listen(ctx, "tcp4", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)))
	if err != nil {
		return fail(4, "loopback listener unavailable")
	}
	defer listener.Close()
	addr, ok := listener.Addr().(*net.TCPAddr)
	if !ok || !addr.IP.IsLoopback() {
		return fail(4, "listener must be loopback TCP")
	}
	if login {
		unregister, e := t.registerView(c)
		if e != nil {
			return e
		}
		defer unregister()
		// Even an ambiguous failed restart must restore readonly.
		defer func() {
			if e := t.restoreView(c); e != nil {
				err = e
			}
		}()
		if err = t.xpra(ctx, c, true); err != nil {
			return err
		}
	}
	if !login {
		unlock()
		unlock = nil
	}
	if demo != "" {
		dir, e := demoDir(demo)
		if e != nil {
			return e
		}
		rctx, stopRecord := context.WithCancel(ctx)
		done := make(chan struct{})
		go func() {
			defer close(done)
			_ = t.record(rctx, c, dir, s.Capsule.Windows)
		}()
		// The recorder writes its stop row and final frame before readonly is restored.
		defer func() { stopRecord(); <-done }()
	}
	socket := t.viewSocket(c)
	handler, closeHandler := newViewHandler(ctx, socket, jquery, listener.Addr().String())
	defer closeHandler()
	server := &http.Server{Handler: handler, ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second, MaxHeaderBytes: 32 << 10, ErrorLog: log.New(io.Discard, "", 0), BaseContext: func(net.Listener) context.Context { return ctx }}
	defer server.Close()
	if err = announce(ViewInfo{"http://" + listener.Addr().String() + "/", "xpra attach socket://" + socket, socket}); err != nil {
		return fail(6, "view announcement unavailable")
	}
	stop := context.AfterFunc(ctx, func() { _ = server.Close() })
	defer stop()
	err = server.Serve(listener)
	if ctx.Err() != nil || errors.Is(err, http.ErrServerClosed) {
		return nil
	}
	return fail(6, "loopback viewer failed")
}
