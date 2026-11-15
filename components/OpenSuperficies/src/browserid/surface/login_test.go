package surface

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
)

type pipeListener struct {
	conns  chan net.Conn
	closed chan struct{}
	once   sync.Once
}

func (l *pipeListener) Accept() (net.Conn, error) {
	select {
	case c := <-l.conns:
		return c, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}
func (l *pipeListener) Close() error { l.once.Do(func() { close(l.closed) }); return nil }
func (l *pipeListener) Addr() net.Addr {
	return &net.TCPAddr{IP: net.ParseIP("127.0.0.1"), Port: 18080}
}

type streamFake struct {
	base        *fakeRunner
	args        chan []string
	restoreFail bool
}

func (r *streamFake) Run(ctx context.Context, a []string, b []byte) ([]byte, error) {
	if r.restoreFail && strings.Contains(strings.Join(a, " "), "SRF_LOGIN=0") {
		return nil, errors.New("restore refused")
	}
	return r.base.Run(ctx, a, b)
}
func (r *streamFake) Stream(ctx context.Context, a []string, in io.Reader, out io.Writer) error {
	r.args <- a
	// Copy an opaque byte and respond; no real network or process.
	b := make([]byte, 1)
	if _, e := io.ReadFull(in, b); e != nil {
		return e
	}
	if _, e := out.Write(b); e != nil {
		return e
	}
	<-ctx.Done()
	return ctx.Err()
}

// The docker-exec relay and its test were retired: the viewer now splices the capsule xpra
// unix socket in-process (view.go, view_test.go covers splice, login restore and its failure).
func TestLoginBindFailureNeverEnablesInput(t *testing.T) {
	tool, r, _, _ := setup(t)
	c := start(t, tool)
	tool.R = &streamFake{base: r}
	tool.Listen = func(context.Context, string, string) (net.Listener, error) { return nil, errors.New("busy") }
	before := len(r.calls)
	if e := tool.LoginAt(context.Background(), c.Container, 18080, func(string) error { return nil }); Code(e) != 4 {
		t.Fatal(e)
	}
	if len(r.calls) != before {
		t.Fatal("changed mode despite bind failure")
	}
}
