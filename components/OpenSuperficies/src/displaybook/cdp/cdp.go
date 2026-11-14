package cdp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/chromedp/cdproto/cdp"
	"github.com/chromedp/cdproto/dom"
	"github.com/chromedp/cdproto/input"
	"github.com/chromedp/cdproto/page"
	"github.com/chromedp/cdproto/runtime"
	"github.com/chromedp/cdproto/target"
	"github.com/coder/websocket"
)

var ErrClientClosed = errors.New("cdp client closed")

type rpcRequest struct {
	ID        int64  `json:"id"`
	SessionID string `json:"sessionId,omitempty"`
	Method    string `json:"method"`
	Params    any    `json:"params,omitempty"`
}

type rpcResponse struct {
	ID     int64           `json:"id"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int64  `json:"code"`
	Message string `json:"message"`
}

// Client is a typed Chrome DevTools Protocol client communicating over WebSocket.
type Client struct {
	wsConn    *websocket.Conn
	sessionID target.SessionID
	targetID  target.ID
	msgID     atomic.Int64
	pendingMu sync.Mutex
	pending   map[int64]chan *rpcResponse
	eventMu   sync.Mutex
	eventChs  map[string][]chan json.RawMessage
	ctx       context.Context
	cancel    context.CancelFunc
	closed    atomic.Bool
}

// Execute satisfies the cdp.Executor interface for cdproto typed commands.
func (c *Client) Execute(ctx context.Context, method string, params any, res any) error {
	if c.closed.Load() {
		return ErrClientClosed
	}

	id := c.msgID.Add(1)
	ch := make(chan *rpcResponse, 1)

	c.pendingMu.Lock()
	c.pending[id] = ch
	c.pendingMu.Unlock()

	defer func() {
		c.pendingMu.Lock()
		delete(c.pending, id)
		c.pendingMu.Unlock()
	}()

	req := rpcRequest{
		ID:     id,
		Method: method,
		Params: params,
	}
	if c.sessionID != "" && !strings.HasPrefix(method, "Target.") {
		req.SessionID = string(c.sessionID)
	}

	data, err := json.Marshal(req)
	if err != nil {
		return fmt.Errorf("marshal cdp request %s: %w", method, err)
	}

	if err := c.wsConn.Write(ctx, websocket.MessageText, data); err != nil {
		return fmt.Errorf("write cdp request %s: %w", method, err)
	}

	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-c.ctx.Done():
		return ErrClientClosed
	case resp := <-ch:
		if resp == nil {
			return ErrClientClosed
		}
		if resp.Error != nil {
			return fmt.Errorf("%s: %s (code %d)", method, resp.Error.Message, resp.Error.Code)
		}
		if res != nil && len(resp.Result) > 0 {
			if err := json.Unmarshal(resp.Result, res); err != nil {
				return fmt.Errorf("unmarshal cdp result %s: %w", method, err)
			}
		}
		return nil
	}
}

// WithContext wraps the context with this Client as the cdp.Executor.
func (c *Client) WithContext(parent context.Context) context.Context {
	return cdp.WithExecutor(parent, c)
}

// SessionID returns the currently attached target session ID.
func (c *Client) SessionID() target.SessionID {
	return c.sessionID
}

// SetSessionID updates the active target session ID.
func (c *Client) SetSessionID(sid target.SessionID) {
	c.sessionID = sid
}

func (c *Client) readLoop() {
	for {
		typ, data, err := c.wsConn.Read(c.ctx)
		if err != nil {
			break
		}
		if typ != websocket.MessageText {
			continue
		}

		var rawMsg struct {
			ID     int64           `json:"id"`
			Method string          `json:"method"`
			Params json.RawMessage `json:"params"`
			Result json.RawMessage `json:"result"`
			Error  *rpcError       `json:"error"`
		}
		if err := json.Unmarshal(data, &rawMsg); err == nil {
			if rawMsg.ID > 0 {
				resp := &rpcResponse{
					ID:     rawMsg.ID,
					Result: rawMsg.Result,
					Error:  rawMsg.Error,
				}
				c.pendingMu.Lock()
				ch, ok := c.pending[resp.ID]
				c.pendingMu.Unlock()
				if ok {
					select {
					case ch <- resp:
					default:
					}
				}
			} else if rawMsg.Method != "" {
				c.eventMu.Lock()
				chs := c.eventChs[rawMsg.Method]
				for _, ch := range chs {
					select {
					case ch <- rawMsg.Params:
					default:
					}
				}
				c.eventMu.Unlock()
			}
		}
	}

	c.pendingMu.Lock()
	for _, ch := range c.pending {
		close(ch)
	}
	c.pending = make(map[int64]chan *rpcResponse)
	c.pendingMu.Unlock()
}

// Close gracefully closes the WebSocket connection.
func (c *Client) Close() error {
	if c.closed.CompareAndSwap(false, true) {
		c.cancel()
		return c.wsConn.Close(websocket.StatusNormalClosure, "client closed")
	}
	return nil
}

// AttachPage searches for a target of type "page" and attaches to it in flattened mode.
func (c *Client) AttachPage(ctx context.Context) error {
	targets, err := target.GetTargets().Do(c.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("target.GetTargets failed: %w", err)
	}

	var pageTarget *target.Info
	for _, t := range targets {
		if t.Type == "page" && !strings.HasPrefix(t.URL, "devtools://") {
			pageTarget = t
			break
		}
	}
	if pageTarget == nil {
		return fmt.Errorf("no target of type page found")
	}

	sid, err := target.AttachToTarget(pageTarget.TargetID).WithFlatten(true).Do(c.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("target.AttachToTarget failed: %w", err)
	}

	c.sessionID = sid
	c.targetID = pageTarget.TargetID

	if err := page.Enable().Do(c.WithContext(ctx)); err != nil {
		return fmt.Errorf("page.Enable failed: %w", err)
	}
	if err := runtime.Enable().Do(c.WithContext(ctx)); err != nil {
		return fmt.Errorf("runtime.Enable failed: %w", err)
	}
	if err := dom.Enable().Do(c.WithContext(ctx)); err != nil {
		return fmt.Errorf("dom.Enable failed: %w", err)
	}

	return nil
}

// Evaluate evaluates a JavaScript expression and returns the raw JSON value.
func (c *Client) Evaluate(ctx context.Context, expr string) ([]byte, error) {
	res, exc, err := runtime.Evaluate(expr).
		WithReturnByValue(true).
		WithAwaitPromise(true).
		Do(c.WithContext(ctx))
	if err != nil {
		return nil, err
	}
	if exc != nil {
		return nil, fmt.Errorf("JS: %s", exc.Text)
	}
	if res == nil {
		return nil, nil
	}
	return []byte(res.Value), nil
}

// OnEvent registers a channel to receive event notifications for the specified CDP method.
// Returns an unregister function.
func (c *Client) OnEvent(method string) (<-chan json.RawMessage, func()) {
	ch := make(chan json.RawMessage, 16)
	c.eventMu.Lock()
	if c.eventChs == nil {
		c.eventChs = make(map[string][]chan json.RawMessage)
	}
	c.eventChs[method] = append(c.eventChs[method], ch)
	c.eventMu.Unlock()

	unsub := func() {
		c.eventMu.Lock()
		defer c.eventMu.Unlock()
		chs := c.eventChs[method]
		for i, existing := range chs {
			if existing == ch {
				c.eventChs[method] = append(chs[:i], chs[i+1:]...)
				break
			}
		}
	}
	return ch, unsub
}

// Navigate navigates the page to the specified URL.
func (c *Client) Navigate(ctx context.Context, targetURL string) error {
	_, _, errorText, _, err := page.Navigate(targetURL).Do(c.WithContext(ctx))
	if err != nil {
		return err
	}
	if errorText != "" {
		return fmt.Errorf("navigate to %s failed: %s", targetURL, errorText)
	}
	return nil
}

// WaitForReady polls document.readyState until it reaches "complete" with a bounded timeout.
// It returns a clear error if the page never becomes ready within timeout or if ctx is cancelled.
func (c *Client) WaitForReady(ctx context.Context, timeout time.Duration) error {
	deadline := time.Now().Add(timeout)
	for {
		raw, err := c.Evaluate(ctx, "document.readyState")
		if err == nil && raw != nil {
			var state string
			if json.Unmarshal(raw, &state) == nil && state == "complete" {
				return nil
			}
		}

		if time.Now().After(deadline) {
			var lastState string
			if raw != nil {
				_ = json.Unmarshal(raw, &lastState)
			}
			return fmt.Errorf("page never became ready: document.readyState was %q after %v", lastState, timeout)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-time.After(25 * time.Millisecond):
		}
	}
}

// urlMatches returns true if currentHref matches targetURL.
// It handles exact matches and trailing slash variations.
func urlMatches(currentHref, targetURL string) bool {
	if currentHref == "" || targetURL == "" {
		return false
	}
	if currentHref == targetURL {
		return true
	}
	if strings.TrimSuffix(currentHref, "/") == strings.TrimSuffix(targetURL, "/") {
		return true
	}
	return false
}

// NavigateAndWait navigates to the target URL and waits for the browser to be on the
// target document and for that document to be loaded (document.readyState == "complete").
// It uses Page.loadEventFired as an accelerator, but requires both document.location.href
// matching targetURL and document.readyState == "complete" before returning.
// Returns a clear error if the page never becomes ready within the bounded timeout.
func (c *Client) NavigateAndWait(ctx context.Context, targetURL string, timeout time.Duration) error {
	loadCh, unsub := c.OnEvent("Page.loadEventFired")
	defer unsub()

	if err := c.Navigate(ctx, targetURL); err != nil {
		return err
	}

	deadline := time.Now().Add(timeout)
	var lastHref, lastState string

	for {
		rawHref, errHref := c.Evaluate(ctx, "document.location.href")
		if errHref != nil || rawHref == nil || json.Unmarshal(rawHref, &lastHref) != nil {
			lastHref = ""
		}

		rawState, errState := c.Evaluate(ctx, "document.readyState")
		if errState != nil || rawState == nil || json.Unmarshal(rawState, &lastState) != nil {
			lastState = ""
		}

		if errHref == nil && errState == nil && urlMatches(lastHref, targetURL) && lastState == "complete" {
			return nil
		}

		if time.Now().After(deadline) {
			if !urlMatches(lastHref, targetURL) {
				return fmt.Errorf("page never became ready: document.location.href was %q (target %q) after %v", lastHref, targetURL, timeout)
			}
			return fmt.Errorf("page never became ready: document.readyState was %q after %v", lastState, timeout)
		}

		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-loadCh:
			// Page.loadEventFired accelerates loop wake-up; href and readyState are strictly checked above.
		case <-time.After(25 * time.Millisecond):
		}
	}
}

// PrimerSelector evaluates candidates in order and returns the first matching selector present in the DOM.
func (c *Client) PrimerSelector(ctx context.Context, candidatos []string) (string, error) {
	for _, sel := range candidatos {
		expr := fmt.Sprintf("!!document.querySelector(%q)", sel)
		val, err := c.Evaluate(ctx, expr)
		if err != nil {
			continue
		}
		var b bool
		if err := json.Unmarshal(val, &b); err == nil && b {
			return sel, nil
		}
	}
	return "", nil
}

// PrimerSelectorVisible evaluates candidates in order and returns the first matching selector
// that is presently visible in the DOM according to CSSOM-view checkVisibility and non-zero bounding box.
func (c *Client) PrimerSelectorVisible(ctx context.Context, candidatos []string) (string, error) {
	for _, sel := range candidatos {
		expr := fmt.Sprintf(`(() => {
			const el = document.querySelector(%q);
			if (!el) return false;
			if (typeof el.checkVisibility === 'function') {
				if (!el.checkVisibility({opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true})) {
					return false;
				}
			} else {
				const st = window.getComputedStyle(el);
				if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') {
					return false;
				}
			}
			const rects = el.getClientRects();
			if (!rects || rects.length === 0) return false;
			for (let i = 0; i < rects.length; i++) {
				if (rects[i].width > 0 && rects[i].height > 0) {
					return true;
				}
			}
			return false;
		})()`, sel)
		val, err := c.Evaluate(ctx, expr)
		if err != nil {
			continue
		}
		var b bool
		if err := json.Unmarshal(val, &b); err == nil && b {
			return sel, nil
		}
	}
	return "", nil
}

// SetFileInputFiles sets input files on the element matching selector.
func (c *Client) SetFileInputFiles(ctx context.Context, selector string, files []string) error {
	doc, err := dom.GetDocument().Do(c.WithContext(ctx))
	if err != nil {
		return fmt.Errorf("DOM.getDocument error: %w", err)
	}

	nodeID, err := dom.QuerySelector(doc.NodeID, selector).Do(c.WithContext(ctx))
	if err != nil || nodeID == 0 {
		return fmt.Errorf("DOM.querySelector para %q fallo: %w", selector, err)
	}

	return dom.SetFileInputFiles(files).WithNodeID(nodeID).Do(c.WithContext(ctx))
}

// InsertText injects text using Input.insertText emulation.
func (c *Client) InsertText(ctx context.Context, text string) error {
	return input.InsertText(text).Do(c.WithContext(ctx))
}

// Connect dials the CDP endpoint (resolving HTTP discovery and bearer authentication) and attaches to the page.
func Connect(ctx context.Context, endpoint, bearerToken string) (*Client, error) {
	wsURL := endpoint
	if strings.HasPrefix(endpoint, "http://") || strings.HasPrefix(endpoint, "https://") {
		versionURL := endpoint
		if !strings.HasSuffix(versionURL, "/json/version") {
			versionURL = strings.TrimSuffix(versionURL, "/") + "/json/version"
		}

		req, err := http.NewRequestWithContext(ctx, http.MethodGet, versionURL, nil)
		if err != nil {
			return nil, fmt.Errorf("create version request: %w", err)
		}
		if bearerToken != "" {
			req.Header.Set("Authorization", "Bearer "+bearerToken)
		}

		httpClient := &http.Client{Timeout: 10 * time.Second}
		resp, err := httpClient.Do(req)
		if err != nil {
			return nil, fmt.Errorf("fetch cdp version from %s: %w", versionURL, err)
		}
		defer resp.Body.Close()
		if resp.StatusCode != http.StatusOK {
			body, _ := io.ReadAll(resp.Body)
			return nil, fmt.Errorf("cdp version returned %d: %s", resp.StatusCode, string(body))
		}

		var v struct {
			WebSocketDebuggerURL string `json:"webSocketDebuggerUrl"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&v); err != nil {
			return nil, fmt.Errorf("decode cdp version: %w", err)
		}
		if v.WebSocketDebuggerURL == "" {
			return nil, fmt.Errorf("empty webSocketDebuggerUrl from %s", versionURL)
		}

		u, err := url.Parse(v.WebSocketDebuggerURL)
		if err == nil {
			endU, err2 := url.Parse(endpoint)
			if err2 == nil && endU.Host != "" {
				u.Host = endU.Host
			}
			wsURL = u.String()
		} else {
			wsURL = v.WebSocketDebuggerURL
		}
	}

	opts := &websocket.DialOptions{}
	if bearerToken != "" {
		opts.HTTPHeader = http.Header{
			"Authorization": []string{"Bearer " + bearerToken},
		}
	}

	conn, _, err := websocket.Dial(ctx, wsURL, opts)
	if err != nil {
		return nil, fmt.Errorf("dial cdp ws %s: %w", wsURL, err)
	}

	clientCtx, clientCancel := context.WithCancel(context.Background())
	c := &Client{
		wsConn:   conn,
		pending:  make(map[int64]chan *rpcResponse),
		eventChs: make(map[string][]chan json.RawMessage),
		ctx:      clientCtx,
		cancel:   clientCancel,
	}
	go c.readLoop()

	if err := c.AttachPage(ctx); err != nil {
		_ = c.Close()
		return nil, fmt.Errorf("attach to page: %w", err)
	}

	return c, nil
}
