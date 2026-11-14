package xpra

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
	"opensuperficies.local/srf/src/dockerapi"
)

func TestProxyHTTPForwarding(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		_, _ = w.Write([]byte("xpra-html5-mock"))
	}))
	defer backend.Close()

	backendHost := backend.Listener.Addr().String()

	proxy, err := NewProxy(ProxyConfig{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: backendHost,
	})
	if err != nil {
		t.Fatalf("failed to create proxy: %v", err)
	}

	req := httptest.NewRequest(http.MethodGet, "/index.html", nil)
	w := httptest.NewRecorder()
	proxy.ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d", w.Code)
	}
	if w.Body.String() != "xpra-html5-mock" {
		t.Fatalf("expected body %q, got %q", "xpra-html5-mock", w.Body.String())
	}
}

func TestProxyTokenAuth(t *testing.T) {
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
	}))
	defer backend.Close()

	backendHost := backend.Listener.Addr().String()
	token := "secure-token-999"

	proxy, err := NewProxy(ProxyConfig{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: backendHost,
		Token:      token,
	})
	if err != nil {
		t.Fatalf("failed to create proxy: %v", err)
	}

	// 1. Missing token -> 401
	reqUnauth := httptest.NewRequest(http.MethodGet, "/", nil)
	wUnauth := httptest.NewRecorder()
	proxy.ServeHTTP(wUnauth, reqUnauth)
	if wUnauth.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 Unauthorized, got %d", wUnauth.Code)
	}

	// 2. Wrong token -> 401
	reqWrong := httptest.NewRequest(http.MethodGet, "/", nil)
	reqWrong.Header.Set("Authorization", "Bearer invalid-token")
	wWrong := httptest.NewRecorder()
	proxy.ServeHTTP(wWrong, reqWrong)
	if wWrong.Code != http.StatusUnauthorized {
		t.Fatalf("expected 401 for bad token, got %d", wWrong.Code)
	}

	// 3. Valid token -> 200
	reqValid := httptest.NewRequest(http.MethodGet, "/", nil)
	reqValid.Header.Set("Authorization", "Bearer "+token)
	wValid := httptest.NewRecorder()
	proxy.ServeHTTP(wValid, reqValid)
	if wValid.Code != http.StatusOK {
		t.Fatalf("expected 200 OK, got %d", wValid.Code)
	}
}

func TestProxyWebSocketEcho(t *testing.T) {
	// Mock backend Xpra WebSocket server
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			InsecureSkipVerify: true,
		})
		if err != nil {
			return
		}
		defer conn.Close(websocket.StatusNormalClosure, "")

		for {
			typ, reader, err := conn.Reader(r.Context())
			if err != nil {
				return
			}
			msg, _ := io.ReadAll(reader)
			writer, err := conn.Writer(r.Context(), typ)
			if err != nil {
				return
			}
			_, _ = writer.Write(append([]byte("echo:"), msg...))
			_ = writer.Close()
		}
	}))
	defer backend.Close()

	backendHost := backend.Listener.Addr().String()

	proxy, err := NewProxy(ProxyConfig{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: backendHost,
	})
	if err != nil {
		t.Fatalf("failed to create proxy: %v", err)
	}

	proxyServer := httptest.NewServer(proxy)
	defer proxyServer.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	wsURL := "ws://" + proxyServer.Listener.Addr().String() + "/xpra"
	clientConn, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("failed to dial proxy websocket: %v", err)
	}
	defer clientConn.Close(websocket.StatusNormalClosure, "")

	payload := []byte("hello-xpra")
	writer, err := clientConn.Writer(ctx, websocket.MessageBinary)
	if err != nil {
		t.Fatalf("failed to create writer: %v", err)
	}
	if _, err := writer.Write(payload); err != nil {
		t.Fatalf("failed to write payload: %v", err)
	}
	if err := writer.Close(); err != nil {
		t.Fatalf("failed to close writer: %v", err)
	}

	_, reader, err := clientConn.Reader(ctx)
	if err != nil {
		t.Fatalf("failed to read response: %v", err)
	}
	response, err := io.ReadAll(reader)
	if err != nil {
		t.Fatalf("failed to read bytes: %v", err)
	}

	expected := "echo:hello-xpra"
	if string(response) != expected {
		t.Fatalf("expected response %q, got %q", expected, string(response))
	}
}

func TestProxyReadOnlyInputFiltering(t *testing.T) {
	backendReceived := make(chan string, 10)
	backend := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := websocket.Accept(w, r, &websocket.AcceptOptions{
			InsecureSkipVerify: true,
		})
		if err != nil {
			return
		}
		defer conn.Close(websocket.StatusNormalClosure, "")

		for {
			_, reader, err := conn.Reader(r.Context())
			if err != nil {
				return
			}
			msg, _ := io.ReadAll(reader)
			backendReceived <- string(msg)
		}
	}))
	defer backend.Close()

	proxy, err := NewProxy(ProxyConfig{
		ListenAddr: "127.0.0.1:0",
		TargetAddr: backend.Listener.Addr().String(),
		ReadOnly:   true,
	})
	if err != nil {
		t.Fatalf("failed to create proxy: %v", err)
	}

	proxyServer := httptest.NewServer(proxy)
	defer proxyServer.Close()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()

	wsURL := "ws://" + proxyServer.Listener.Addr().String() + "/xpra"
	clientConn, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatalf("failed to dial: %v", err)
	}
	defer clientConn.Close(websocket.StatusNormalClosure, "")

	// In read-only mode, client input packets are dropped
	writer, _ := clientConn.Writer(ctx, websocket.MessageBinary)
	_, _ = writer.Write([]byte("click-event"))
	_ = writer.Close()

	select {
	case msg := <-backendReceived:
		t.Fatalf("unexpected message received by backend in read-only mode: %s", msg)
	case <-time.After(300 * time.Millisecond):
		// Expected: message was dropped by proxy
	}

	// Now switch to operator mode (ReadOnly = false)
	proxy.SetReadOnly(false)
	writer2, _ := clientConn.Writer(ctx, websocket.MessageBinary)
	_, _ = writer2.Write([]byte("operator-click"))
	_ = writer2.Close()

	select {
	case msg := <-backendReceived:
		if msg != "operator-click" {
			t.Fatalf("expected operator-click, got %s", msg)
		}
	case <-time.After(1 * time.Second):
		t.Fatalf("timed out waiting for operator message")
	}
}

type mockDockerConfig struct {
	execExitCode    int
	createExecDelay time.Duration
	execDelay       time.Duration
	execStdout      string
	estadoReadOnly  bool
	failCreateExec  bool
	failHijack      bool
}

func startMockDocker(t *testing.T, cfg mockDockerConfig) (string, func()) {
	t.Helper()
	tmpDir, err := os.MkdirTemp("", "mock-docker-*")
	if err != nil {
		t.Fatalf("failed to create temp dir: %v", err)
	}
	sockPath := filepath.Join(tmpDir, "docker.sock")
	l, err := net.Listen("unix", sockPath)
	if err != nil {
		_ = os.RemoveAll(tmpDir)
		t.Fatalf("failed to listen on unix: %v", err)
	}

	var execCount int
	mux := http.NewServeMux()
	mux.HandleFunc("/containers/", func(w http.ResponseWriter, r *http.Request) {
		if cfg.failCreateExec {
			http.Error(w, `{"message":"No such container"}`, http.StatusNotFound)
			return
		}
		if cfg.createExecDelay > 0 && execCount == 0 {
			time.Sleep(cfg.createExecDelay)
		}
		execCount++
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"Id": fmt.Sprintf("exec-%d", execCount),
		})
	})
	mux.HandleFunc("/exec/", func(w http.ResponseWriter, r *http.Request) {
		path := r.URL.Path
		if strings.HasSuffix(path, "/start") {
			if cfg.failHijack {
				http.Error(w, "hijack failed", http.StatusInternalServerError)
				return
			}
			hj, ok := w.(http.Hijacker)
			if !ok {
				http.Error(w, "hijack unsupported", http.StatusInternalServerError)
				return
			}
			conn, bufrw, err := hj.Hijack()
			if err != nil {
				return
			}
			defer conn.Close()

			if cfg.execDelay > 0 && strings.Contains(path, "exec-1") {
				time.Sleep(cfg.execDelay)
			}

			_, _ = bufrw.WriteString("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
			_ = bufrw.Flush()

			var payload []byte
			if strings.Contains(path, "exec-1") {
				if cfg.execStdout != "" {
					payload = []byte(cfg.execStdout)
				} else {
					payload = []byte("xpra stopped\nxpra started\n")
				}
			} else {
				// Estado check
				if cfg.estadoReadOnly {
					payload = []byte("1234 ? S 0:00 xpra start-desktop :100 --readonly=yes\n")
				} else {
					payload = []byte("1234 ? S 0:00 xpra start-desktop :100 --readonly=no\n")
				}
			}
			_ = dockerapi.WriteFrame(bufrw, dockerapi.StreamStdout, payload)
			_ = bufrw.Flush()
			return
		}
		if strings.HasSuffix(path, "/json") {
			w.WriteHeader(http.StatusOK)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"ID":       "exec-mock",
				"Running":  false,
				"ExitCode": cfg.execExitCode,
			})
			return
		}
		http.NotFound(w, r)
	})

	server := &http.Server{Handler: mux}
	go func() {
		_ = server.Serve(l)
	}()

	cleanup := func() {
		_ = server.Close()
		_ = l.Close()
		_ = os.RemoveAll(tmpDir)
	}
	return sockPath, cleanup
}

func TestConmutacionResultJSONAndZeroValues(t *testing.T) {
	// 1. Marshalling and unmarshalling contract for intervened mode
	resIntervenir := ConmutacionResult{
		Slot:       3,
		Contenedor: "srf-ng-3",
		ReadOnly:   false,
		MS:         542,
		Estado:     "intervened",
	}
	data, err := json.Marshal(resIntervenir)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}
	var decodedIntervenir ConmutacionResult
	if err := json.Unmarshal(data, &decodedIntervenir); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}
	if decodedIntervenir != resIntervenir {
		t.Fatalf("roundtrip mismatch: got %+v, want %+v", decodedIntervenir, resIntervenir)
	}

	// 2. Marshalling and unmarshalling contract for shielded mode
	resBlindar := ConmutacionResult{
		Slot:       5,
		Contenedor: "srf-ng-5",
		ReadOnly:   true,
		MS:         518,
		Estado:     "shielded",
	}
	dataB, err := json.Marshal(resBlindar)
	if err != nil {
		t.Fatalf("marshal error: %v", err)
	}
	var decodedBlindar ConmutacionResult
	if err := json.Unmarshal(dataB, &decodedBlindar); err != nil {
		t.Fatalf("unmarshal error: %v", err)
	}
	if decodedBlindar != resBlindar {
		t.Fatalf("roundtrip mismatch: got %+v, want %+v", decodedBlindar, resBlindar)
	}

	// 3. Zero-value semantics
	var zeroStruct ConmutacionResult
	zeroData, err := json.Marshal(zeroStruct)
	if err != nil {
		t.Fatalf("marshal zero struct error: %v", err)
	}
	expectedZeroJSON := `{"slot":0,"contenedor":"","readonly":false,"ms":0,"estado":""}`
	if string(zeroData) != expectedZeroJSON {
		t.Fatalf("zero-value json mismatch: got %s, want %s", string(zeroData), expectedZeroJSON)
	}

	var fromEmptyJSON ConmutacionResult
	if err := json.Unmarshal([]byte(`{}`), &fromEmptyJSON); err != nil {
		t.Fatalf("unmarshal empty json error: %v", err)
	}
	if fromEmptyJSON != zeroStruct {
		t.Fatalf("empty json unmarshal mismatch: got %+v, want %+v", fromEmptyJSON, zeroStruct)
	}
}

// TestConmutarReadBackDisagreementRegressionF2 proves U09-F2 and A2-03.
// When Conmutar is requested with readOnly=false, but the container's actual state is readonly=true,
// the result ReadOnly MUST reflect the read-back state (true), disagreeing with the requested flag.
// If reverted to echoing the input argument (ReadOnly: readOnly), this test FAILS.
// Furthermore, Estado MUST derive from actualRO ("shielded"), never contradicting ReadOnly (A2-03).
func TestConmutarReadBackDisagreementRegressionF2(t *testing.T) {
	sock, cleanup := startMockDocker(t, mockDockerConfig{
		estadoReadOnly: true, // Container remains shielded (readonly=yes)
	})
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	// Request readOnly=false (intervenir)
	res, err := ConmutarWithOptions(ctx, 0, false, ConmutarOptions{
		DockerSocket: sock,
	})
	if err != nil {
		t.Fatalf("ConmutarWithOptions failed: %v", err)
	}

	// MUST reflect read-back state (true), NOT the requested argument (false)
	if res.ReadOnly != true {
		t.Fatalf("U09-F2 regression: ReadOnly was %v (echoed input?), expected true from read-back state", res.ReadOnly)
	}
	// A2-03 regression: Estado must match read-back ReadOnly
	if res.Estado != "shielded" {
		t.Fatalf("A2-03 regression: Estado was %q (echoed input 'intervened'?), expected 'shielded'", res.Estado)
	}
}

// TestConmutarFullSpanTimingRegressionF3 proves U09-F3 and A2-05.
// The reported MS must cover the FULL span from before CreateExec to after switch completion.
// By injecting delay strictly in CreateExec (before StartExecHijack), this test FAILS if
// wallStart is moved past CreateExec (A2-05 fix).
func TestConmutarFullSpanTimingRegressionF3(t *testing.T) {
	sock, cleanup := startMockDocker(t, mockDockerConfig{
		createExecDelay: 60 * time.Millisecond,
		execStdout:      "SRF_XPRA_MS:5\n", // script claims 5ms
	})
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	res, err := ConmutarWithOptions(ctx, 0, false, ConmutarOptions{
		DockerSocket: sock,
	})
	if err != nil {
		t.Fatalf("ConmutarWithOptions failed: %v", err)
	}

	// Full span must measure the real wall clock including CreateExec delay (>= 50ms),
	// proving wallStart precedes CreateExec.
	if res.MS < 50 {
		t.Fatalf("U09-F3/A2-05 regression: MS was %d ms (wallStart moved past CreateExec?), expected >= 50 ms full span", res.MS)
	}
}

// TestParseXpraEstadoFalsifiabilityA201 proves A2-01.
// When xpra output is empty, absent, or unparseable, parseXpraEstado MUST return a non-nil error.
// If reverted to returning (true, nil), this test FAILS.
func TestParseXpraEstadoFalsifiabilityA201(t *testing.T) {
	testCases := []struct {
		name    string
		output  string
		wantErr bool
	}{
		{"empty string", "", true},
		{"whitespace only", "   \n\t  \n", true},
		{"non-xpra processes only", "1234 ? S 0:00 /usr/bin/openbox\n5678 ? S 0:00 chromium --remote-debugging-port=9222\n", true},
		{"xpra without start-desktop", "1234 ? S 0:00 xpra --version\n", true},
		{"xpra start-desktop without readonly flag", "1234 ? S 0:00 xpra start-desktop :100 --use-display\n", true},
		{"xpra start-desktop with conflicting flags", "1234 ? S 0:00 xpra start-desktop :100 --readonly=yes --readonly=no\n", true},
		{"valid readonly=yes", "1234 ? S 0:00 xpra start-desktop :100 --use-display --readonly=yes\n", false},
		{"valid readonly=no", "1234 ? S 0:00 xpra start-desktop :100 --use-display --readonly=no\n", false},
	}

	for _, tc := range testCases {
		t.Run(tc.name, func(t *testing.T) {
			ro, err := parseXpraEstado(tc.output)
			if tc.wantErr && err == nil {
				t.Fatalf("A2-01 regression: expected error for %q, got readonly=%v with nil error (unfalsifiable default)", tc.name, ro)
			}
			if !tc.wantErr && err != nil {
				t.Fatalf("unexpected error for %q: %v", tc.name, err)
			}
		})
	}
}

// TestParseXpraEstadoDecoyResistanceA202 proves A2-02.
// Decoy processes containing --readonly=yes must NOT override a real server running with --readonly=no.
// If reverted to substring match over entire output, this test FAILS.
func TestParseXpraEstadoDecoyResistanceA202(t *testing.T) {
	// Case 1: Decoy process with --readonly=yes alongside real xpra with --readonly=no
	decoyOutput := "1234 ? S 0:00 xpra start-desktop :100 --use-display --readonly=no\n" +
		"9999 ? S 0:00 python3 /opt/decoy.py --readonly=yes\n"
	ro, err := parseXpraEstado(decoyOutput)
	if err != nil {
		t.Fatalf("unexpected error with decoy: %v", err)
	}
	if ro != false {
		t.Fatalf("A2-02 regression: got readonly=%v, expected false (decoy --readonly=yes spoofed output)", ro)
	}

	// Case 2: PIDFILE output correctly identifies real server despite decoy in process list
	pidfileOutput := "PIDFILE:xpra start-desktop :100 --use-display --readonly=no\n" +
		"8888 ? S 0:00 xpra start-desktop :100 --readonly=yes\n"
	ro2, err := parseXpraEstado(pidfileOutput)
	if err != nil {
		t.Fatalf("unexpected error with pidfile: %v", err)
	}
	if ro2 != false {
		t.Fatalf("A2-02 regression: got readonly=%v from PIDFILE line, expected false", ro2)
	}

	// Case 3: Multiple ambiguous xpra start-desktop processes without PIDFILE must return error under A2-01
	multiOutput := "1234 ? S 0:00 xpra start-desktop :100 --use-display --readonly=no\n" +
		"5678 ? S 0:00 xpra start-desktop :100 --use-display --readonly=yes\n"
	_, errMulti := parseXpraEstado(multiOutput)
	if errMulti == nil {
		t.Fatalf("A2-02 regression: expected error for multiple ambiguous xpra processes, got nil")
	}
}

// TestEstadoConsistencyA203 proves A2-03.
// When container read-back reports readonly=false, Estado MUST be "intervened".
// When container read-back reports readonly=true, Estado MUST be "shielded".
// They must never contradict.
func TestEstadoConsistencyA203(t *testing.T) {
	// Case 1: Request blindar (readOnly=true), but container is intervened (readonly=false)
	sockFalse, cleanupFalse := startMockDocker(t, mockDockerConfig{
		estadoReadOnly: false,
	})
	defer cleanupFalse()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	resBlindar, err := ConmutarWithOptions(ctx, 0, true, ConmutarOptions{DockerSocket: sockFalse})
	if err != nil {
		t.Fatalf("ConmutarWithOptions failed: %v", err)
	}
	if resBlindar.ReadOnly != false {
		t.Fatalf("expected ReadOnly=false, got %v", resBlindar.ReadOnly)
	}
	if resBlindar.Estado != "intervened" {
		t.Fatalf("A2-03 regression: expected Estado='intervened' matching ReadOnly=false, got %q", resBlindar.Estado)
	}

	// Case 2: Request intervenir (readOnly=false), but container is shielded (readonly=true)
	sockTrue, cleanupTrue := startMockDocker(t, mockDockerConfig{
		estadoReadOnly: true,
	})
	defer cleanupTrue()

	resIntervenir, err := ConmutarWithOptions(ctx, 0, false, ConmutarOptions{DockerSocket: sockTrue})
	if err != nil {
		t.Fatalf("ConmutarWithOptions failed: %v", err)
	}
	if resIntervenir.ReadOnly != true {
		t.Fatalf("expected ReadOnly=true, got %v", resIntervenir.ReadOnly)
	}
	if resIntervenir.Estado != "shielded" {
		t.Fatalf("A2-03 regression: expected Estado='shielded' matching ReadOnly=true, got %q", resIntervenir.Estado)
	}
}

func TestConmutarExecFailureErrorHandling(t *testing.T) {
	sock, cleanup := startMockDocker(t, mockDockerConfig{
		execExitCode: 2, // simulated script exit code failure
	})
	defer cleanup()

	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	_, err := ConmutarWithOptions(ctx, 0, false, ConmutarOptions{
		DockerSocket: sock,
	})
	if err == nil {
		t.Fatalf("expected error on exec non-zero exit code, got nil")
	}
	if !strings.Contains(err.Error(), "code 2") {
		t.Fatalf("expected error to mention exit code 2, got: %v", err)
	}
}

func TestConmutarContextCancellation(t *testing.T) {
	sock, cleanup := startMockDocker(t, mockDockerConfig{
		execDelay: 500 * time.Millisecond,
	})
	defer cleanup()

	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel immediately

	_, err := ConmutarWithOptions(ctx, 0, false, ConmutarOptions{
		DockerSocket: sock,
	})
	if err == nil {
		t.Fatalf("expected error on canceled context, got nil")
	}
}

func TestConmutarNonexistentContainer(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	_, err := Conmutar(ctx, 9999, false)
	if err == nil {
		t.Fatalf("expected error for nonexistent container, got nil")
	}
}
