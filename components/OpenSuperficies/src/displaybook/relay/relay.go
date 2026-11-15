package relay

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"time"
)

// Config defines the in-container CDP relay parameters.
type Config struct {
	ListenAddr string
	TargetAddr string
	PublicAddr string
	Token      string
}

// Relay forwards authenticated HTTP and WebSocket traffic to loopback Chromium.
type Relay struct {
	cfg        Config
	server     *http.Server
	httpClient *http.Client
	listener   net.Listener
	mu         sync.Mutex
	closed     bool
}

// NewRelay constructs an initialized Relay instance.
func NewRelay(cfg Config) (*Relay, error) {
	if strings.TrimSpace(cfg.Token) == "" {
		return nil, fmt.Errorf("token is required (fail-closed)")
	}
	if cfg.ListenAddr == "" {
		cfg.ListenAddr = "0.0.0.0:9220"
	}
	if cfg.TargetAddr == "" {
		cfg.TargetAddr = "127.0.0.1:9222"
	}
	if cfg.PublicAddr == "" {
		host, port, err := net.SplitHostPort(cfg.ListenAddr)
		if err == nil {
			if host == "" || host == "0.0.0.0" || host == "::" {
				host = "127.0.0.1"
			}
			cfg.PublicAddr = net.JoinHostPort(host, port)
		} else {
			cfg.PublicAddr = cfg.ListenAddr
		}
	}

	r := &Relay{
		cfg: cfg,
		httpClient: &http.Client{
			Timeout: 15 * time.Second,
		},
	}

	r.server = &http.Server{
		Addr:    cfg.ListenAddr,
		Handler: r,
	}

	return r, nil
}

// Start listens on the configured address and serves requests.
func (r *Relay) Start() error {
	ln, err := net.Listen("tcp", r.cfg.ListenAddr)
	if err != nil {
		return fmt.Errorf("relay listen on %s failed: %w", r.cfg.ListenAddr, err)
	}
	r.mu.Lock()
	r.listener = ln
	r.mu.Unlock()
	return r.server.Serve(ln)
}

// Addr returns the actual bound network address, or empty if not yet listening.
func (r *Relay) Addr() string {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.listener != nil {
		return r.listener.Addr().String()
	}
	return r.cfg.ListenAddr
}

// Close gracefully terminates the relay server and listener.
func (r *Relay) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil
	}
	r.closed = true
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	return r.server.Shutdown(ctx)
}

// ServeHTTP enforces bearer authentication and routes HTTP or WebSocket requests.
func (r *Relay) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	// 1. Authenticate bearer token strictly from Authorization header (fail-closed, no query param)
	token := ""
	auth := req.Header.Get("Authorization")
	if strings.HasPrefix(auth, "Bearer ") {
		token = strings.TrimPrefix(auth, "Bearer ")
	}

	if r.cfg.Token == "" || subtle.ConstantTimeCompare([]byte(token), []byte(r.cfg.Token)) != 1 {
		w.Header().Set("WWW-Authenticate", `Bearer realm="srf-relay"`)
		http.Error(w, "unauthorized", http.StatusUnauthorized)
		return
	}

	// 2. Handle WebSocket upgrade
	if isWebSocketUpgrade(req) {
		r.serveWebSocket(w, req)
		return
	}

	// 3. Handle discovery requests and any /json endpoints with URL rewriting
	if strings.HasPrefix(req.URL.Path, "/json") {
		r.serveDiscovery(w, req)
		return
	}

	// 4. Default HTTP proxying
	r.serveProxy(w, req)
}

func isWebSocketUpgrade(req *http.Request) bool {
	connHeader := strings.ToLower(req.Header.Get("Connection"))
	upgradeHeader := strings.ToLower(req.Header.Get("Upgrade"))
	return strings.Contains(connHeader, "upgrade") && upgradeHeader == "websocket"
}

func (r *Relay) serveWebSocket(w http.ResponseWriter, req *http.Request) {
	hj, ok := w.(http.Hijacker)
	if !ok {
		http.Error(w, "webserver does not support hijacking", http.StatusInternalServerError)
		return
	}

	clientConn, clientBuf, err := hj.Hijack()
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}
	defer clientConn.Close()

	targetConn, err := net.DialTimeout("tcp", r.cfg.TargetAddr, 5*time.Second)
	if err != nil {
		return
	}
	defer targetConn.Close()

	// Clone and rewrite request for target
	targetReq := req.Clone(req.Context())
	targetReq.URL.Scheme = "http"
	targetReq.URL.Host = r.cfg.TargetAddr
	targetReq.Host = r.cfg.TargetAddr
	targetReq.Header.Del("Authorization")

	if err := targetReq.Write(targetConn); err != nil {
		return
	}

	if clientBuf.Reader.Buffered() > 0 {
		buffered := make([]byte, clientBuf.Reader.Buffered())
		if _, err := io.ReadFull(clientBuf, buffered); err == nil {
			_, _ = targetConn.Write(buffered)
		}
	}

	errc := make(chan error, 2)
	go func() {
		_, err := io.Copy(targetConn, clientConn)
		errc <- err
	}()
	go func() {
		_, err := io.Copy(clientConn, targetConn)
		errc <- err
	}()
	<-errc
}

func (r *Relay) serveDiscovery(w http.ResponseWriter, req *http.Request) {
	targetURL := "http://" + r.cfg.TargetAddr + req.URL.RequestURI()
	treq, err := http.NewRequestWithContext(req.Context(), req.Method, targetURL, req.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	for k, vv := range req.Header {
		if strings.EqualFold(k, "Authorization") {
			continue
		}
		for _, v := range vv {
			treq.Header.Add(k, v)
		}
	}
	treq.Host = r.cfg.TargetAddr

	resp, err := r.httpClient.Do(treq)
	if err != nil {
		http.Error(w, fmt.Sprintf("target unreachable: %v", err), http.StatusBadGateway)
		return
	}
	defer resp.Body.Close()

	bodyBytes, err := io.ReadAll(resp.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	// U08-F3: Rewrite to configured PublicAddr, ignoring untrusted client req.Host
	publicHost := r.cfg.PublicAddr
	if publicHost == "" {
		publicHost = "127.0.0.1:9220"
	}

	rewritten := RewriteCDPDiscoveryJSON(bodyBytes, r.cfg.TargetAddr, publicHost)

	for k, vv := range resp.Header {
		if strings.EqualFold(k, "Content-Length") {
			continue
		}
		for _, v := range vv {
			w.Header().Add(k, v)
		}
	}
	w.Header().Set("Content-Length", fmt.Sprintf("%d", len(rewritten)))
	w.WriteHeader(resp.StatusCode)
	_, _ = w.Write(rewritten)
}

func (r *Relay) serveProxy(w http.ResponseWriter, req *http.Request) {
	targetURL := "http://" + r.cfg.TargetAddr + req.URL.RequestURI()
	treq, err := http.NewRequestWithContext(req.Context(), req.Method, targetURL, req.Body)
	if err != nil {
		http.Error(w, err.Error(), http.StatusInternalServerError)
		return
	}

	for k, vv := range req.Header {
		if strings.EqualFold(k, "Authorization") {
			continue
		}
		for _, v := range vv {
			treq.Header.Add(k, v)
		}
	}
	treq.Host = r.cfg.TargetAddr

	resp, err := r.httpClient.Do(treq)
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

// RewriteCDPDiscoveryJSON replaces target loopback addresses in discovery payloads with the public host.
func RewriteCDPDiscoveryJSON(data []byte, targetAddr, publicHost string) []byte {
	// First attempt clean JSON deserialization
	var obj interface{}
	if err := json.Unmarshal(data, &obj); err == nil {
		rewriteJSONValue(&obj, targetAddr, publicHost)
		if serialized, err := json.Marshal(obj); err == nil {
			return serialized
		}
	}

	// Fallback byte replacement
	raw := string(data)
	if targetAddr != "" {
		raw = strings.ReplaceAll(raw, "ws://"+targetAddr, "ws://"+publicHost)
		raw = strings.ReplaceAll(raw, "wss://"+targetAddr, "wss://"+publicHost)
		raw = strings.ReplaceAll(raw, "ws="+targetAddr, "ws="+publicHost)
		raw = strings.ReplaceAll(raw, "wss="+targetAddr, "wss="+publicHost)
	}
	raw = strings.ReplaceAll(raw, "ws://127.0.0.1:9222", "ws://"+publicHost)
	raw = strings.ReplaceAll(raw, "ws://localhost:9222", "ws://"+publicHost)
	raw = strings.ReplaceAll(raw, "wss://127.0.0.1:9222", "wss://"+publicHost)
	raw = strings.ReplaceAll(raw, "wss://localhost:9222", "wss="+publicHost)
	raw = strings.ReplaceAll(raw, "ws=127.0.0.1:9222", "ws="+publicHost)
	raw = strings.ReplaceAll(raw, "ws=localhost:9222", "ws="+publicHost)
	raw = strings.ReplaceAll(raw, "wss=127.0.0.1:9222", "wss="+publicHost)
	raw = strings.ReplaceAll(raw, "wss=localhost:9222", "wss="+publicHost)
	return []byte(raw)
}

func rewriteJSONValue(val *interface{}, targetAddr, publicHost string) {
	if val == nil || *val == nil {
		return
	}
	switch v := (*val).(type) {
	case map[string]interface{}:
		for k, item := range v {
			if k == "webSocketDebuggerUrl" {
				if s, ok := item.(string); ok {
					v[k] = rewriteWSURL(s, targetAddr, publicHost)
				}
			} else if k == "devtoolsFrontendUrl" {
				if s, ok := item.(string); ok {
					v[k] = rewriteFrontendURL(s, targetAddr, publicHost)
				}
			} else if s, ok := item.(string); ok {
				if strings.Contains(s, "127.0.0.1:9222") || (targetAddr != "" && strings.Contains(s, targetAddr)) {
					s = strings.ReplaceAll(s, "127.0.0.1:9222", publicHost)
					if targetAddr != "" {
						s = strings.ReplaceAll(s, targetAddr, publicHost)
					}
					v[k] = s
				}
			} else {
				rewriteJSONValue(&item, targetAddr, publicHost)
				v[k] = item
			}
		}
	case []interface{}:
		for i := range v {
			rewriteJSONValue(&v[i], targetAddr, publicHost)
		}
	}
}

func rewriteWSURL(originalWS, targetAddr, publicHost string) string {
	u, err := url.Parse(originalWS)
	if err != nil {
		res := originalWS
		if targetAddr != "" {
			res = strings.ReplaceAll(res, targetAddr, publicHost)
		}
		res = strings.ReplaceAll(res, "127.0.0.1:9222", publicHost)
		res = strings.ReplaceAll(res, "localhost:9222", publicHost)
		return res
	}
	u.Host = publicHost
	return u.String()
}

func rewriteFrontendURL(originalURL, targetAddr, publicHost string) string {
	res := originalURL
	if targetAddr != "" {
		res = strings.ReplaceAll(res, "ws="+targetAddr, "ws="+publicHost)
		res = strings.ReplaceAll(res, "wss="+targetAddr, "wss="+publicHost)
	}
	res = strings.ReplaceAll(res, "ws=127.0.0.1:9222", "ws="+publicHost)
	res = strings.ReplaceAll(res, "ws=localhost:9222", "ws="+publicHost)
	res = strings.ReplaceAll(res, "wss=127.0.0.1:9222", "wss="+publicHost)
	res = strings.ReplaceAll(res, "wss=localhost:9222", "wss="+publicHost)
	return res
}
