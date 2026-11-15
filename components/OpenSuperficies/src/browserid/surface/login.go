package surface

import (
	"context"
	"net"
)

type ListenFunc func(context.Context, string, string) (net.Listener, error)

func (t Tool) xpra(ctx context.Context, c string, login bool) error {
	mode := "0"
	if login {
		mode = "1"
	}
	_, e := t.R.Run(ctx, []string{"docker", "exec", "-u", "10001:10001", "-e", "SRF_LOGIN=" + mode, c, "timeout", "--signal=TERM", "--kill-after=1s", "120s", "/usr/local/bin/identity-entrypoint.sh", "xpra-start"}, nil)
	if e != nil {
		return fail(6, "xpra mode change failed")
	}
	return nil
}

// Login is the operator's interactive view alias.
func (t Tool) Login(ctx context.Context, c string) (any, error) {
	var info ViewInfo
	err := t.ViewAt(ctx, c, 0, true, func(v ViewInfo) error { info = v; return nil })
	if err != nil {
		return nil, err
	}
	return info, nil
}

func (t Tool) LoginAt(ctx context.Context, c string, port int, announce func(string) error) error {
	if announce == nil {
		return fail(3, "URL callback required")
	}
	return t.ViewAt(ctx, c, port, true, func(v ViewInfo) error { return announce(v.URL) })
}
