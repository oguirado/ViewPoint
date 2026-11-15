package cdp

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/coder/websocket"
)

type mockCDPServer struct {
	token       string
	mu          sync.Mutex
	evalHandler func(expr string) (any, error)
}

func newMockCDPServer(token string) *mockCDPServer {
	return &mockCDPServer{
		token: token,
	}
}

func (s *mockCDPServer) serveHTTP(w http.ResponseWriter, r *http.Request) {
	if s.token != "" {
		auth := r.Header.Get("Authorization")
		if auth != "Bearer "+s.token {
			http.Error(w, "unauthorized", http.StatusUnauthorized)
			return
		}
	}

	if strings.HasSuffix(r.URL.Path, "/json/version") {
		w.Header().Set("Content-Type", "application/json")
		host := r.Host
		if host == "" {
			host = "127.0.0.1"
		}
		_ = json.NewEncoder(w).Encode(map[string]string{
			"Browser":              "Chrome/152.0.0.0",
			"webSocketDebuggerUrl": "ws://" + host + "/devtools/browser/test",
		})
		return
	}

	conn, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer conn.Close(websocket.StatusNormalClosure, "mock closed")

	for {
		typ, data, err := conn.Read(r.Context())
		if err != nil {
			break
		}
		if typ != websocket.MessageText {
			continue
		}

		var req rpcRequest
		if err := json.Unmarshal(data, &req); err != nil {
			continue
		}

		resp := rpcResponse{
			ID: req.ID,
		}

		switch req.Method {
		case "Target.getTargets":
			resp.Result, _ = json.Marshal(map[string]any{
				"targetInfos": []map[string]any{
					{
						"targetId": "target-page-1",
						"type":     "page",
						"url":      "about:blank",
					},
				},
			})
		case "Target.attachToTarget":
			resp.Result, _ = json.Marshal(map[string]any{
				"sessionId": "session-page-1",
			})
		case "Page.enable", "Runtime.enable", "DOM.enable", "Page.navigate", "Input.insertText", "Input.dispatchKeyEvent":
			resp.Result = json.RawMessage(`{}`)
		case "DOM.getDocument":
			resp.Result = json.RawMessage(`{"root":{"nodeId":1}}`)
		case "DOM.querySelector":
			resp.Result = json.RawMessage(`{"nodeId":2}`)
		case "DOM.setFileInputFiles":
			resp.Result = json.RawMessage(`{}`)
		case "Runtime.evaluate":
			var params struct {
				Expression string `json:"expression"`
			}
			if b, ok := req.Params.(json.RawMessage); ok {
				_ = json.Unmarshal(b, &params)
			} else if m, ok := req.Params.(map[string]any); ok {
				if expr, ok := m["expression"].(string); ok {
					params.Expression = expr
				}
			}

			s.mu.Lock()
			handler := s.evalHandler
			s.mu.Unlock()

			var val any = true
			if handler != nil {
				res, err := handler(params.Expression)
				if err != nil {
					resp.Error = &rpcError{Code: -32000, Message: err.Error()}
					val = nil
				} else {
					val = res
				}
			}

			if resp.Error == nil {
				valJSON, _ := json.Marshal(val)
				resp.Result, _ = json.Marshal(map[string]any{
					"result": map[string]any{
						"type":  "object",
						"value": json.RawMessage(valJSON),
					},
				})
			}
		default:
			resp.Result = json.RawMessage(`{}`)
		}

		respData, _ := json.Marshal(resp)
		_ = conn.Write(r.Context(), websocket.MessageText, respData)
	}
}

func TestClientConnectAndRPC(t *testing.T) {
	mock := newMockCDPServer("test-bearer-token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	// 1. Unauthenticated connect should fail
	_, err := Connect(ctx, ts.URL, "wrong-token")
	if err == nil {
		t.Fatalf("expected connect to fail with wrong token")
	}

	// 2. Authenticated connect succeeds
	client, err := Connect(ctx, ts.URL, "test-bearer-token")
	if err != nil {
		t.Fatalf("connect failed: %v", err)
	}
	defer client.Close()

	if client.SessionID() != "session-page-1" {
		t.Fatalf("expected session ID session-page-1, got %s", client.SessionID())
	}

	// 3. Navigate
	if err := client.Navigate(ctx, "about:blank"); err != nil {
		t.Fatalf("navigate failed: %v", err)
	}

	// 4. Evaluate expression
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return "hello-cdp", nil
	}
	mock.mu.Unlock()

	raw, err := client.Evaluate(ctx, "window.test")
	if err != nil {
		t.Fatalf("evaluate failed: %v", err)
	}
	var str string
	if err := json.Unmarshal(raw, &str); err != nil || str != "hello-cdp" {
		t.Fatalf("expected hello-cdp, got %s (err %v)", string(raw), err)
	}
}

func TestGateAuth(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"login_cta": {"#login-cta"},
	}

	// Case 1: login_cta present -> ErrHumano
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "#login-cta") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	errAuth := GateAuth(ctx, client, sel)
	if errAuth == nil {
		t.Fatalf("expected GateAuth to fail when login_cta is visible")
	}
	var eh *ErrHumano
	if !errors.As(errAuth, &eh) || !strings.Contains(errAuth.Error(), "G-AUTH") {
		t.Fatalf("expected G-AUTH error message, got %v", errAuth)
	}

	// Case 2: login_cta absent -> nil
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return false, nil
	}
	mock.mu.Unlock()

	if err := GateAuth(ctx, client, sel); err != nil {
		t.Fatalf("expected GateAuth to pass, got: %v", err)
	}
}

func TestGateHuman(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"desafio": {"#challenge-running"},
	}

	// Case 1: challenge present -> ErrHumano
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "#challenge-running") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	errHuman := GateHuman(ctx, client, sel)
	if errHuman == nil {
		t.Fatalf("expected GateHuman to fail when challenge is present")
	}
	if !strings.Contains(errHuman.Error(), "G-HUMAN") {
		t.Fatalf("expected G-HUMAN error message, got %v", errHuman)
	}

	// Case 2: challenge absent -> nil
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return false, nil
	}
	mock.mu.Unlock()

	if err := GateHuman(ctx, client, sel); err != nil {
		t.Fatalf("expected GateHuman to pass, got %v", err)
	}
}

func TestGateSubmit(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
		"detener":      {"#stopBtn"},
	}

	// Case 1: Stream appears -> true
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": true}, nil
		}
		if strings.Contains(expr, "#submitBtn") || strings.Contains(expr, "#stream") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos int
	ok, err := GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if err != nil || !ok {
		t.Fatalf("expected GateSubmit to succeed, ok=%v, err=%v", ok, err)
	}
	if despachos != 1 {
		t.Fatalf("expected despachos == 1, got %d", despachos)
	}

	// Case 2: Stream never appears -> false (ambiguous)
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": true}, nil
		}
		if strings.Contains(expr, "#submitBtn") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos2 int
	ok, err = GateSubmit(ctx, client, sel, 150*time.Millisecond, &despachos2)
	if err != nil || ok {
		t.Fatalf("expected GateSubmit to return false on timeout, ok=%v, err=%v", ok, err)
	}
}

// OBJ-6 / HAL-U13-37: Truncated chip with aria-label MUST be accepted.
func TestGateAttachAriaLabel(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "test-...md",
			Errores:   nil,
			EdLen:     10,
			Pendiente: false,
			ChipFound: true,
			ChipAria:  "test-fixture.md",
			ChipTitle: "test-fixture.md",
			ChipText:  "test-...md",
		}, nil
	}
	mock.mu.Unlock()

	if err := GateAttach(ctx, client, nil, "test-fixture.md", 1*time.Second); err != nil {
		t.Fatalf("expected GateAttach to succeed with aria-label chip resolution, got: %v", err)
	}
}

// OBJ-6 / HAL-U13-37: Truncated chip with title MUST be accepted.
func TestGateAttachTitle(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "test-...md",
			Errores:   nil,
			EdLen:     10,
			Pendiente: false,
			ChipFound: true,
			ChipTitle: "test-fixture.md",
			ChipText:  "test-...md",
		}, nil
	}
	mock.mu.Unlock()

	if err := GateAttach(ctx, client, nil, "test-fixture.md", 1*time.Second); err != nil {
		t.Fatalf("expected GateAttach to succeed with title chip resolution, got: %v", err)
	}
}

// Mutator: Truncated chip without aria-label or title must NOT be accepted.
func TestGateAttachTruncatedWithoutAria(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "test-...md",
			Errores:   nil,
			EdLen:     10,
			Pendiente: false,
			ChipFound: false, // Not found because truncated without aria/title
			ChipText:  "test-...md",
		}, nil
	}
	mock.mu.Unlock()

	errAttach := GateAttach(ctx, client, nil, "test-fixture.md", 300*time.Millisecond)
	if errAttach == nil {
		t.Fatalf("expected GateAttach to fail when chip is truncated without aria-label or title")
	}
	if !strings.Contains(errAttach.Error(), "nunca se asento") {
		t.Fatalf("expected timeout settling error, got: %v", errAttach)
	}
}

// G-ATTACH condition 2: Rejects generic degradation to paste.txt.
func TestGateAttachGenericPasteTxt(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "file paste.txt uploaded",
			Errores:   nil,
			EdLen:     10,
			Pendiente: false,
			ChipFound: true,
			ChipText:  "paste.txt",
		}, nil
	}
	mock.mu.Unlock()

	errAttach := GateAttach(ctx, client, nil, "test-fixture.md", 1*time.Second)
	if errAttach == nil {
		t.Fatalf("expected GateAttach to fail on paste.txt degradation")
	}
	if !strings.Contains(errAttach.Error(), "adjunto generico") {
		t.Fatalf("expected generic attachment rejection, got: %v", errAttach)
	}
}

// G-ATTACH condition 1: Rejects upload error alerts.
func TestGateAttachErrorAlert(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "File upload failed",
			Errores:   []string{"File size too large"},
			EdLen:     10,
			Pendiente: false,
			ChipFound: false,
		}, nil
	}
	mock.mu.Unlock()

	errAttach := GateAttach(ctx, client, nil, "test-fixture.md", 1*time.Second)
	if errAttach == nil {
		t.Fatalf("expected GateAttach to fail on upload error alerts")
	}
	if !strings.Contains(errAttach.Error(), "error de subida") {
		t.Fatalf("expected upload error message, got: %v", errAttach)
	}
}

// G-ATTACH condition 3: Rejects editor absorption (> 4000 characters).
func TestGateAttachEditorOverflow(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		return GateAttachStatus{
			Texto:     "document text",
			Errores:   nil,
			EdLen:     4500, // > 4000
			Pendiente: false,
			ChipFound: true,
			ChipAria:  "test-fixture.md",
		}, nil
	}
	mock.mu.Unlock()

	errAttach := GateAttach(ctx, client, nil, "test-fixture.md", 1*time.Second)
	if errAttach == nil {
		t.Fatalf("expected GateAttach to fail on editor overflow (>4000 chars)")
	}
	if !strings.Contains(errAttach.Error(), "caracteres sentados en el editor") {
		t.Fatalf("expected editor overflow message, got: %v", errAttach)
	}
}

// Regression test for U11-F1: WaitForReady succeeds when readyState reaches "complete".
func TestWaitForReadySuccess(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	callCount := 0
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "document.readyState") {
			callCount++
			if callCount < 2 {
				return "loading", nil
			}
			return "complete", nil
		}
		return true, nil
	}
	mock.mu.Unlock()

	if err := client.WaitForReady(ctx, 1*time.Second); err != nil {
		t.Fatalf("expected WaitForReady to succeed, got: %v", err)
	}
}

// Regression test for U11-F1: WaitForReady fails deterministically with clear error
// when page never reaches "complete". FAILS if fix is reverted (e.g. sleep/no check).
func TestWaitForReadyTimeout(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "document.readyState") {
			return "loading", nil // never reaches complete
		}
		return true, nil
	}
	mock.mu.Unlock()

	t0 := time.Now()
	err = client.WaitForReady(ctx, 150*time.Millisecond)
	elapsed := time.Since(t0)

	if err == nil {
		t.Fatalf("expected WaitForReady to fail on never-ready page, got nil")
	}
	if !strings.Contains(err.Error(), "page never became ready") {
		t.Fatalf("expected clear page never became ready error, got: %v", err)
	}
	if elapsed < 120*time.Millisecond {
		t.Fatalf("timeout returned prematurely: elapsed %v", elapsed)
	}
}

// Regression test for U11-F2: despachos is a real counter, incremented at the dispatch point.
// FAILS if fix is reverted (e.g. literal assignment despachos = 1).
func TestGateSubmitDespachosCounter(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
	}

	mock.mu.Lock()
	inPageCount := 0
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			inPageCount++
			return map[string]any{"ok": true, "despachos": inPageCount}, nil
		}
		if strings.Contains(expr, "#submitBtn") || strings.Contains(expr, "#stream") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos int

	// 1st dispatch attempt
	ok, err := GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if err != nil || !ok {
		t.Fatalf("GateSubmit attempt 1 failed: ok=%v, err=%v", ok, err)
	}
	if despachos != 1 {
		t.Fatalf("expected despachos == 1 after first dispatch, got %d", despachos)
	}

	// 2nd dispatch attempt: MUST increment to 2, proving it is a counter and not a literal
	ok, err = GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if err != nil || !ok {
		t.Fatalf("GateSubmit attempt 2 failed: ok=%v, err=%v", ok, err)
	}
	if despachos != 2 {
		t.Fatalf("expected despachos == 2 after second dispatch (real counter), got %d", despachos)
	}
}

// Regression test for A-08: challenge path stops before dispatch; GateSubmit re-evaluates
// the challenge gate at dispatch time and fails with *ErrHumano, leaving despachos at 0.
// DISCRIMINATING: fails (incrementing despachos to 1 and returning ok=true) if GateSubmit
// omits the synchronous G-HUMAN guard.
func TestGateHumanNoDispatch(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"desafio":      {"#challenge-running"},
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
	}

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		// Discriminating mock:
		// If expr checks for G-HUMAN / desafioSels, and challenge is present: halt with G-HUMAN
		if strings.Contains(expr, `gate: "G-HUMAN"`) {
			return map[string]any{"ok": false, "gate": "G-HUMAN", "reason": "desafio interactivo visible"}, nil
		}
		// If expr contains submitter.click() without the G-HUMAN guard, the click proceeds!
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": true, "despachos": 1}, nil
		}
		if strings.Contains(expr, "#challenge-running") {
			return true, nil
		}
		if strings.Contains(expr, "#submitBtn") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	// Initial G-HUMAN gate fails
	errHuman := GateHuman(ctx, client, sel)
	if errHuman == nil {
		t.Fatalf("expected GateHuman to fail on challenge")
	}

	// Discriminating test for A-08: if GateSubmit is called directly when challenge is present,
	// it MUST re-evaluate G-HUMAN in the dispatch expression, halt with ErrHumano, and NOT increment despachos.
	var despachos int
	ok, errSubmit := GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if ok || errSubmit == nil {
		t.Fatalf("expected GateSubmit to fail when challenge is present, got ok=%v, err=%v", ok, errSubmit)
	}
	var eh *ErrHumano
	if !errors.As(errSubmit, &eh) {
		t.Fatalf("expected *ErrHumano from GateSubmit on challenge, got %v", errSubmit)
	}
	if despachos != 0 {
		t.Fatalf("despachos must remain 0 on challenge path, got %d", despachos)
	}
}

// Regression test for U-11 Attempt 3 defect: NavigateAndWait must NOT satisfy the wait
// when document.readyState is "complete" for the WRONG document (e.g. cold start on about:blank).
// It must require BOTH document.location.href matching targetURL AND document.readyState == "complete".
// FAILS if the fix is reverted (i.e. returning on readyState complete or loadEventFired without URL match).
func TestNavigateAndWaitColdStartWrongDocument(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	const (
		wrongURL  = "about:blank"
		targetURL = "file:///uploads/fixture.html"
	)

	var (
		mu            sync.Mutex
		calls         int
		targetReached bool
	)

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		mu.Lock()
		defer mu.Unlock()

		if strings.Contains(expr, "document.location.href") {
			calls++
			if calls < 3 {
				// Cold-start model: first 2 evaluations still see about:blank
				return wrongURL, nil
			}
			// Navigation commits and lands on target document
			targetReached = true
			return targetURL, nil
		}

		if strings.Contains(expr, "document.readyState") {
			// Cold-start defect model: readyState is ALREADY "complete" on about:blank!
			// If URL is not checked, a buggy implementation returns immediately.
			return "complete", nil
		}

		return true, nil
	}
	mock.mu.Unlock()

	err = client.NavigateAndWait(ctx, targetURL, 1*time.Second)
	if err != nil {
		t.Fatalf("NavigateAndWait failed: %v", err)
	}

	mu.Lock()
	reached := targetReached
	totalCalls := calls
	mu.Unlock()

	if !reached {
		t.Fatalf("NavigateAndWait returned while still on wrong document (%q) - cold start race defect! total href checks: %d", wrongURL, totalCalls)
	}
	if totalCalls < 3 {
		t.Fatalf("expected at least 3 href checks before target was committed, got %d", totalCalls)
	}
}

// Regression test: NavigateAndWait times out with a clear error when target document readyState never becomes complete.
func TestNavigateAndWaitTimeoutNeverReady(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	const targetURL = "file:///uploads/fixture.html"

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "document.location.href") {
			return targetURL, nil
		}
		if strings.Contains(expr, "document.readyState") {
			return "loading", nil
		}
		return true, nil
	}
	mock.mu.Unlock()

	t0 := time.Now()
	err = client.NavigateAndWait(ctx, targetURL, 150*time.Millisecond)
	elapsed := time.Since(t0)

	if err == nil {
		t.Fatalf("expected NavigateAndWait to fail on never-ready page, got nil")
	}
	if !strings.Contains(err.Error(), "page never became ready") {
		t.Fatalf("expected clear page never became ready error, got: %v", err)
	}
	if !strings.Contains(err.Error(), "document.readyState was \"loading\"") {
		t.Fatalf("expected error to include document.readyState was \"loading\", got: %v", err)
	}
	if elapsed < 120*time.Millisecond {
		t.Fatalf("timeout returned prematurely: elapsed %v", elapsed)
	}
}

// Regression test: NavigateAndWait times out with a clear error when browser never lands on target URL.
func TestNavigateAndWaitTimeoutWrongDocument(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	const (
		wrongURL  = "about:blank"
		targetURL = "file:///uploads/fixture.html"
	)

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "document.location.href") {
			return wrongURL, nil
		}
		if strings.Contains(expr, "document.readyState") {
			return "complete", nil
		}
		return true, nil
	}
	mock.mu.Unlock()

	t0 := time.Now()
	err = client.NavigateAndWait(ctx, targetURL, 150*time.Millisecond)
	elapsed := time.Since(t0)

	if err == nil {
		t.Fatalf("expected NavigateAndWait to fail on wrong document, got nil")
	}
	if !strings.Contains(err.Error(), "page never became ready") {
		t.Fatalf("expected clear page never became ready error, got: %v", err)
	}
	if !strings.Contains(err.Error(), "document.location.href was \"about:blank\"") {
		t.Fatalf("expected error to include location href, got: %v", err)
	}
	if elapsed < 120*time.Millisecond {
		t.Fatalf("timeout returned prematurely: elapsed %v", elapsed)
	}
}

// TestAssertionA01LateChallengeGuarded verifies Assertion A-01:
// A challenge appearing after initial gate check is detected at dispatch time by GateSubmit,
// halting execution with *ErrHumano and leaving despachos at 0.
func TestAssertionA01LateChallengeGuarded(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"desafio":      {"#challenge-running"},
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
	}

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		// Single synchronous dispatch expression checks G-HUMAN:
		// If G-HUMAN guard is present and challenge element is in DOM, halt immediately
		if strings.Contains(expr, `gate: "G-HUMAN"`) {
			return map[string]any{"ok": false, "gate": "G-HUMAN", "reason": "desafio interactivo visible"}, nil
		}
		// If G-HUMAN check was omitted from GateSubmit, click proceeds and increments despachos
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": true, "despachos": 1}, nil
		}
		if strings.Contains(expr, "#challenge-running") {
			return true, nil
		}
		if strings.Contains(expr, "#submitBtn") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos int
	ok, errSubmit := GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if ok || errSubmit == nil {
		t.Fatalf("expected GateSubmit to fail when late challenge is detected at dispatch, ok=%v, err=%v", ok, errSubmit)
	}
	var eh *ErrHumano
	if !errors.As(errSubmit, &eh) {
		t.Fatalf("expected *ErrHumano, got %T (%v)", errSubmit, errSubmit)
	}
	if despachos != 0 {
		t.Fatalf("despachos must be 0 on late challenge, got %d", despachos)
	}
}

// TestAssertionA02BodyTextOnlyRejected verifies Assertion A-02:
// A page whose body text merely names the file with no chip or empty FileList
// must NOT be accepted as an attachment (gates.attach == false).
// DISCRIMINATING: fails if native FileList check is deleted or if body text is treated as a chip.
func TestAssertionA02BodyTextOnlyRejected(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		// Simulate DOM with input[type=file] present but empty FileList,
		// and filename mentioned in body text but no .chip elements.
		var errores []string
		if strings.Contains(expr, `errores.push("input[type=file] has empty FileList")`) {
			errores = append(errores, "input[type=file] has empty FileList")
		}

		chipFound := false
		if strings.Contains(expr, "chipFound = true") && strings.Contains(expr, "t.includes") {
			chipFound = true
		}

		return GateAttachStatus{
			Texto:     "File attached in body text: test-fixture.md",
			Errores:   errores,
			EdLen:     10,
			Pendiente: false,
			ChipFound: chipFound,
		}, nil
	}
	mock.mu.Unlock()

	errAttach := GateAttach(ctx, client, nil, "test-fixture.md", 200*time.Millisecond)
	if errAttach == nil {
		t.Fatalf("expected GateAttach to reject body-text-only attachment, got nil")
	}
	if !strings.Contains(errAttach.Error(), "empty FileList") {
		t.Fatalf("expected error containing 'empty FileList', got: %v", errAttach)
	}
}

// TestAssertionA03IncompleteStreamNonZero verifies Assertion A-03:
// When a stream never reaches completion, completo is false and the return code
// is non-zero (specifically RCSinFin = 4), never RCOk (0).
// DISCRIMINATING: tests production mapping function EvaluarSalidaStream used by cmd/srf-motor.
func TestAssertionA03IncompleteStreamNonZero(t *testing.T) {
	rcIncomplete, errIncomplete := EvaluarSalidaStream(false)
	if rcIncomplete == 0 {
		t.Fatalf("incomplete stream must have non-zero rc, got %d", rcIncomplete)
	}
	if rcIncomplete != RCSinFin {
		t.Fatalf("expected RCSinFin (%d), got %d", RCSinFin, rcIncomplete)
	}
	if errIncomplete == nil {
		t.Fatalf("expected non-nil error for incomplete stream")
	}

	rcComplete, errComplete := EvaluarSalidaStream(true)
	if rcComplete != RCOk {
		t.Fatalf("complete stream must have RCOk (%d), got %d", RCOk, rcComplete)
	}
	if errComplete != nil {
		t.Fatalf("expected nil error for complete stream, got %v", errComplete)
	}
}

// TestAssertionA04InvisibleConfirmationAmbiguous verifies Assertion A-04:
// When #stream exists in the DOM but is display:none, PrimerSelectorVisible does not
// accept it, and GateSubmit returns false, nil (ambiguous, rc=6), not rc=0.
// DISCRIMINATING: fails (returning ok=true) if GateSubmit used presence-based PrimerSelector instead.
func TestAssertionA04InvisibleConfirmationAmbiguous(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
		"detener":      {"#stopBtn"},
	}

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": true, "despachos": 1}, nil
		}
		if strings.Contains(expr, "#submitBtn") {
			return true, nil
		}
		// In invisible confirmation fixture: #stream exists in DOM but checkVisibility returns false.
		// A presence check (!!document.querySelector) returns true; a visibility check returns false.
		if strings.Contains(expr, "#stream") {
			if strings.Contains(expr, "checkVisibility") || strings.Contains(expr, "rects[i].width") {
				return false, nil // Invisible!
			}
			return true, nil // Present in DOM!
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos int
	ok, errSubmit := GateSubmit(ctx, client, sel, 200*time.Millisecond, &despachos)
	if errSubmit != nil {
		t.Fatalf("expected no transport error, got %v", errSubmit)
	}
	if ok {
		t.Fatalf("expected GateSubmit to return false (ambiguous rc=6) when confirmation is invisible, got ok=true")
	}
	if despachos != 1 {
		t.Fatalf("expected despachos == 1 (dispatch was attempted), got %d", despachos)
	}
}

// TestAssertionA05DespachosNonVariadicSinglePoint verifies Assertion A-05:
// despachos is a required, non-variadic parameter incremented at exactly one point
// (the dispatch point) and never incremented when a pre-condition fails.
func TestAssertionA05DespachosNonVariadicSinglePoint(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	sel := map[string][]string{
		"boton_enviar": {"#submitBtn"},
		"stream":       {"#stream"},
	}

	// 0. Nil guard check
	_, errNil := GateSubmit(ctx, client, sel, 100*time.Millisecond, nil)
	if errNil == nil {
		t.Fatalf("expected error when despachos is nil")
	}

	// 1. When pre-condition fails: despachos must NOT increment
	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			return map[string]any{"ok": false, "gate": "G-SUBMIT", "reason": "button disabled"}, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	var despachos int
	ok, errSubmit := GateSubmit(ctx, client, sel, 100*time.Millisecond, &despachos)
	if ok || errSubmit == nil {
		t.Fatalf("expected failure on disabled button")
	}
	if despachos != 0 {
		t.Fatalf("despachos must remain 0 when dispatch pre-condition fails, got %d", despachos)
	}

	// 2. When dispatch succeeds: despachos must increment by exactly 1 (read back from in-page counter)
	mock.mu.Lock()
	inPageCount := 0
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "submitter.click()") {
			inPageCount++
			return map[string]any{"ok": true, "despachos": inPageCount}, nil
		}
		if strings.Contains(expr, "#stream") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	ok, errSubmit = GateSubmit(ctx, client, sel, 1*time.Second, &despachos)
	if errSubmit != nil || !ok {
		t.Fatalf("expected success, ok=%v, err=%v", ok, errSubmit)
	}
	if despachos != 1 {
		t.Fatalf("despachos must be incremented to 1, got %d", despachos)
	}
}

// TestPrimerSelectorVisible tests that PrimerSelectorVisible correctly discriminates
// between elements that are merely present in the DOM and elements that are actually visible.
func TestPrimerSelectorVisible(t *testing.T) {
	mock := newMockCDPServer("token")
	ts := httptest.NewServer(http.HandlerFunc(mock.serveHTTP))
	defer ts.Close()

	ctx := context.Background()
	client, err := Connect(ctx, ts.URL, "token")
	if err != nil {
		t.Fatalf("connect: %v", err)
	}
	defer client.Close()

	mock.mu.Lock()
	mock.evalHandler = func(expr string) (any, error) {
		if strings.Contains(expr, "#hidden-element") {
			// Presence check (!!document.querySelector) returns true
			if !strings.Contains(expr, "checkVisibility") && !strings.Contains(expr, "getClientRects") {
				return true, nil
			}
			// Visibility check (checkVisibility / rects) returns false
			return false, nil
		}
		if strings.Contains(expr, "#visible-element") {
			return true, nil
		}
		return false, nil
	}
	mock.mu.Unlock()

	// 1. Presence-only PrimerSelector accepts #hidden-element
	presenceSel, err := client.PrimerSelector(ctx, []string{"#hidden-element"})
	if err != nil || presenceSel != "#hidden-element" {
		t.Fatalf("expected PrimerSelector to find #hidden-element by presence, got %q, %v", presenceSel, err)
	}

	// 2. PrimerSelectorVisible MUST reject #hidden-element
	visSel, err := client.PrimerSelectorVisible(ctx, []string{"#hidden-element"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if visSel != "" {
		t.Fatalf("expected PrimerSelectorVisible to reject hidden element, got %q", visSel)
	}

	// 3. PrimerSelectorVisible accepts #visible-element
	visSel, err = client.PrimerSelectorVisible(ctx, []string{"#visible-element"})
	if err != nil || visSel != "#visible-element" {
		t.Fatalf("expected PrimerSelectorVisible to accept #visible-element, got %q, %v", visSel, err)
	}
}

