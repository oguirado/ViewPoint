package xpra

import (
	"context"
	"crypto/subtle"
	"fmt"
	"io"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/coder/websocket"
)

// ProxyConfig defines the WebSocket and HTTP proxy parameters for an Xpra endpoint.
type ProxyConfig struct {
	ListenAddr string
	TargetAddr string
	Token      string
	ReadOnly   bool
}

// Proxy bridges HTTP and WebSocket traffic between client frontend surfaces and Xpra backends.
type Proxy struct {
	cfg        ProxyConfig
	server     *http.Server
	httpClient *http.Client
	listener   net.Listener
	mu         sync.Mutex
	closed     bool
	readOnly   atomic.Bool
}

// NewProxy constructs an initialized Xpra Proxy instance.
func NewProxy(cfg ProxyConfig) (*Proxy, error) {
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = "127.0.0.1:0"
	}
	if cfg.TargetAddr == "" {
		return nil, fmt.Errorf("xpra proxy: TargetAddr is required")
	}

	p := &Proxy{
		cfg: cfg,
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
		},
	}
	p.readOnly.Store(cfg.ReadOnly)

	p.server = &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: p,
	}

	return p, nil
}

// Start opens the network listener and serves HTTP/WebSocket connections.
func (p *Proxy) Start() error {
	ln, err := net.Listen("tcp", p.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("xpra proxy listen failed on %s: %w", p.cfg.ListenAddr, err)
	}
	p.mu.Lock()
	p.listener = ln
	p.mu.Unlock()
	return p.server.Serve(ln)
}

// Addr returns the bound network address of the proxy.
func (p *Proxy) Addr() string {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.listener != nil {
		return p.listener.Addr().String()
	}
	return p.cfg.ListenAddr
}

// SetReadOnly toggles proxy-side input filtering.
func (p *Proxy) SetReadOnly(ro bool) {
	p.readOnly.Store(ro)
}

// IsReadOnly queries current proxy-side input filtering state.
func (p *Proxy) IsReadOnly() bool {
	return p.readOnly.Load()
}

// Close gracefully terminates the proxy server.
func (p *Proxy) Close() error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.closed {
		return nil
	}
	p.closed = true
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return p.server.Shutdown(ctx)
}

// ServeHTTP authenticates requests and dispatches to WebSocket or HTTP reverse proxy.
func (p *Proxy) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	// Authenticate Bearer token if configured
	if p.cfg.Token != "" {
		auth := req.Header.Get("Authorization")
		token := ""
		if strings.HasPrefix(auth, "Bearer ") {
			token = strings.TrimPrefix(auth, "Bearer ")
		}
		if subtle.ConstantTimeCompare([]byte(token), []byte(p.cfg.Token)) != 1 {
			w.Header().Set("WWW-Authenticate", `Bearer realm="srf-xpra"`)
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
	}

	if isWebSocketUpgrade(req) {
		p.serveWebSocket(w, req)
		return
	}

	p.serveHTTP(w, req)
}

func isWebSocketUpgrade(req *http.Request) bool {
	connHeader := strings.ToLower(req.Header.Get("Connection"))
	upgradeHeader := strings.ToLower(req.Header.Get("Upgrade"))
	return strings.Contains(connHeader, "upgrade") && upgradeHeader == "websocket"
}

func (p *Proxy) serveWebSocket(w http.ResponseWriter, req *http.Request) {
	clientConn, err := websocket.Accept(w, req, &websocket.AcceptOptions{
		InsecureSkipVerify: true,
		CompressionMode:    websocket.CompressionDisabled,
	})
	if err != nil {
		return
	}
	defer clientConn.Close(websocket.StatusInternalError, "")

	ctx := req.Context()
	targetURL := "ws://" + p.cfg.TargetAddr + req.URL.RequestURI()

	dialOpts := &websocket.DialOptions{
		CompressionMode: websocket.CompressionDisabled,
	}
	targetConn, _, err := websocket.Dial(ctx, targetURL, dialOpts)
	if err != nil {
		clientConn.Close(websocket.StatusBadGateway, "failed to dial target xpra server")
		return
	}
	defer targetConn.Close(websocket.StatusInternalError, "")

	errc := make(chan error, 2)

	// Forward client input packets to target backend (filterable on readOnly)
	go func() {
		for {
			typ, reader, err := clientConn.Reader(ctx)
			if err != nil {
				errc <- err
				return
			}
			if p.IsReadOnly() {
				// Discard client input packets in read-only mode
				_, _ = io.Copy(io.Discard, reader)
				continue
			}
			writer, err := targetConn.Writer(ctx, typ)
			if err != nil {
				errc <- err
				return
			}
			if _, err := io.Copy(writer, reader); err != nil {
				_ = writer.Close()
				errc <- err
				return
			}
			if err := writer.Close(); err != nil {
				errc <- err
				return
			}
		}
	}()

	// Forward target framebuffer updates to client surface
	go func() {
		for {
			typ, reader, err := targetConn.Reader(ctx)
			if err != nil {
				errc <- err
				return
			}
			writer, err := clientConn.Writer(ctx, typ)
			if err != nil {
				errc <- err
				return
			}
			if _, err := io.Copy(writer, reader); err != nil {
				_ = writer.Close()
				errc <- err
				return
			}
			if err := writer.Close(); err != nil {
				errc <- err
				return
			}
		}
	}()

	select {
	case <-ctx.Done():
	case <-errc:
	}
}

func (p *Proxy) serveHTTP(w http.ResponseWriter, req *http.Request) {
	targetURL := "http://" + p.cfg.TargetAddr + req.URL.RequestURI()
	treq, err := http.NewRequestWithContext(req.Context(), req.Method, targetURL, req.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	for k, vv := range req.Header {
		if strings.EqualFold(k, "Authorization") && p.cfg.Token != "" {
			continue
		}
		for _, v := range vv {
			treq.Header.Add(k, v)
		}
	}
	treq.Host = p.cfg.TargetAddr

	resp, err := p.httpClient.Do(treq)
	if err != nil {
		http.Error(w, fmt.Sprintf("target unreachable: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	for k, vv := range resp.Header {
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.WriteHeader(resp.StatusCode)
	_, _ = io.Copy(w, resp.Body)
}
