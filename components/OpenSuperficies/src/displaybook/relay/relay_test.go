package relay

import (
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestRelayUnauthorizedRejection(t *testing.T) {
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: "127.0.0.1:9999",
		Token:      "secret-token-1234",
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/json/version", nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected status 401 for unauthenticated request, got %d", w.Code)
	}
}

func TestRelayWrongToken(t *testing.T) {
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: "127.0.0.1:9999",
		Token:      "secret-token-1234",
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/json/version", nil)
	req.Header.Set("Authorization", "Bearer wrong-token")
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected status 401 for invalid token, got %d", w.Code)
	}
}

func TestRelayDiscoveryRewriting(t *testing.T) {
	mockBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/json/version" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"Browser": "Chrome/120.0.0.0",
				"Protocol-Version": "1.3",
				"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/abc-123"
			}`))
			return
		}
		http.NotFound(w, req)
	}))
	defer mockBackend.Close()

	backendHost := mockBackend.Listener.Addr().String()
	token := "auth-token-test"

	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		PublicAddr: "127.0.0.1:9220",
		TargetAddr: backendHost,
		Token:      token,
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/json/version", nil)
	req.Host = "127.0.0.1:9220"
	req.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", w.Code, w.Body.String())
	}

	var res map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatalf("failed to unmarshal JSON: %v", err)
	}

	if res["Protocol-Version"] != "1.3" {
		t.Fatalf("expected Protocol-Version 1.3, got %v", res["Protocol-Version"])
	}

	wsURL, ok := res["webSocketDebuggerUrl"].(string)
	if !ok || !strings.Contains(wsURL, "127.0.0.1:9220") {
		t.Fatalf("expected webSocketDebuggerUrl to contain 127.0.0.1:9220, got %s", wsURL)
	}
}

func TestRelayWebSocketProxy(t *testing.T) {
	// Mock backend WS server
	mockBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		conn, err := websocket.Accept(w, req, &websocket.AcceptOptions{
			InsecureSkipVerify: true,
		})
		if err != nil {
			return
		}
		defer conn.Close(websocket.StatusNormalClosure, "")

		for {
			typ, reader, err := conn.Reader(req.Context())
			if err != nil {
				return
			}
			msg, _ := io.ReadAll(reader)
			writer, err := conn.Writer(req.Context(), typ)
			if err != nil {
				return
			}
			_, _ = writer.Write(append([]byte("echo: "), msg...))
			_ = writer.Close()
		}
	}))
	defer mockBackend.Close()

	backendHost := mockBackend.Listener.Addr().String()
	token := "ws-token-xyz"

	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: backendHost,
		Token:      token,
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	relayServer := httptest.NewServer(r)
	defer relayServer.Close()

	relayURL := "ws://" + relayServer.Listener.Addr().String() + "/devtools/page/1"
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 1. Unauthenticated WS dial should fail
	_, _, err = websocket.Dial(ctx, relayURL, nil)
	if err == nil {
		t.Fatalf("expected unauthenticated WS dial to fail")
	}

	// 2. Authenticated WS dial should succeed
	opts := &websocket.DialOptions{
		HTTPHeader: http.Header{
			"Authorization": []string{"Bearer " + token},
		},
	}
	wsConn, _, err := websocket.Dial(ctx, relayURL, opts)
	if err != nil {
		t.Fatalf("authenticated WS dial failed: %v", err)
	}
	defer wsConn.Close(websocket.StatusNormalClosure, "")

	if err := wsConn.Write(ctx, websocket.MessageText, []byte("hello-cdp")); err != nil {
		t.Fatalf("failed to write WS message: %v", err)
	}

	typ, respBytes, err := wsConn.Read(ctx)
	if err != nil {
		t.Fatalf("failed to read WS response: %v", err)
	}
	if typ != websocket.MessageText || string(respBytes) != "echo: hello-cdp" {
		t.Fatalf("unexpected WS message: %s", string(respBytes))
	}
}

// Regression test for U08-F1: relay must fail-closed if token is empty
func TestRelayEmptyTokenFatal(t *testing.T) {
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: "127.0.0.1:9999",
		Token:      "",
	}
	r, err := NewRelay(cfg)
	if err == nil {
		_ = r.Close()
		t.Fatalf("expected NewRelay with empty token to fail closed, got nil error")
	}
}

// Regression test for U08-F5: token in query string (?token=) must be rejected
func TestRelayRejectsQueryParamToken(t *testing.T) {
	token := "valid-secret-token"
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: "127.0.0.1:9999",
		Token:      token,
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/json/version?token="+token, nil)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 Unauthorized for query param token, got %d", w.Code)
	}
}

// Regression test for U08-F3: discovery rewrite must use configured PublicAddr and ignore client Host header
func TestRelayDiscoveryRewritingIgnoresClientHost(t *testing.T) {
	mockBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{
			"Browser": "Chrome/120.0.0.0",
			"Protocol-Version": "1.3",
			"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/xyz"
		}`))
	}))
	defer mockBackend.Close()

	token := "auth-token-test"
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		PublicAddr: "127.0.0.1:9220",
		TargetAddr: mockBackend.Listener.Addr().String(),
		Token:      token,
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/json/version", nil)
	req.Host = "injected.attacker.domain:8080"
	req.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", w.Code, w.Body.String())
	}

	var res map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatalf("failed to unmarshal JSON: %v", err)
	}

	wsURL, ok := res["webSocketDebuggerUrl"].(string)
	if !ok {
		t.Fatalf("missing webSocketDebuggerUrl in response")
	}
	if strings.Contains(wsURL, "injected.attacker.domain") {
		t.Fatalf("webSocketDebuggerUrl leaked untrusted client Host header: %s", wsURL)
	}
	if !strings.Contains(wsURL, "127.0.0.1:9220") {
		t.Fatalf("expected webSocketDebuggerUrl to carry configured address 127.0.0.1:9220, got: %s", wsURL)
	}
}

// Regression test for U08-F4: rewrite covers /json/new, wss=, and other endpoints
func TestRelayDiscoveryRewritingJsonNewAndWSS(t *testing.T) {
	mockBackend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, req *http.Request) {
		if req.URL.Path == "/json/new" {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{
				"id": "target-456",
				"type": "page",
				"devtoolsFrontendUrl": "/devtools/inspector.html?wss=127.0.0.1:9222/devtools/page/target-456",
				"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/target-456"
			}`))
			return
		}
		http.NotFound(w, req)
	}))
	defer mockBackend.Close()

	token := "auth-token-test"
	cfg := Config{
		ListenAddr: "127.0.0.1:0",
		PublicAddr: "127.0.0.1:9220",
		TargetAddr: mockBackend.Listener.Addr().String(),
		Token:      token,
	}
	r, err := NewRelay(cfg)
	if err != nil {
		t.Fatalf("failed to create relay: %v", err)
	}

	req := httptest.NewRequest(http.MethodPut, "/json/new", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected status 200, got %d: %s", w.Code, w.Body.String())
	}

	bodyStr := w.Body.String()
	if strings.Contains(bodyStr, "127.0.0.1:9222") {
		t.Fatalf("response leaked loopback CDP address 127.0.0.1:9222: %s", bodyStr)
	}

	var res map[string]interface{}
	if err := json.Unmarshal(w.Body.Bytes(), &res); err != nil {
		t.Fatalf("failed to unmarshal JSON: %v", err)
	}

	wsURL, ok := res["webSocketDebuggerUrl"].(string)
	if !ok || !strings.Contains(wsURL, "127.0.0.1:9220") {
		t.Fatalf("expected webSocketDebuggerUrl to carry 127.0.0.1:9220, got: %s", wsURL)
	}

	frontendURL, ok := res["devtoolsFrontendUrl"].(string)
	if !ok || !strings.Contains(frontendURL, "wss=127.0.0.1:9220") {
		t.Fatalf("expected devtoolsFrontendUrl to carry wss=127.0.0.1:9220, got: %s", frontendURL)
	}
}
