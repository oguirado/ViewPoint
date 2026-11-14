// srf-egress-proxy runs only the egress forward proxy; it never creates networks
// or containers. The operator supplies the reviewed policy and IP registry.
package main

import (
	"context"
	"encoding/json"
	"flag"
	"io"
	"log"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"opensuperficies.local/srf/src/browserid/proxy"
)

func main() { os.Exit(run(os.Args[1:], os.Stdout)) }
func run(args []string, out io.Writer) int {
	fs := flag.NewFlagSet("srf-egress-proxy", flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	listen := fs.String("listen", "", "internal interface address")
	allowAny := fs.Bool("allow-any-listen", false, "explicitly permit wildcard listen IP")
	registry := fs.String("registry", "", "capsule IP registry JSON")
	policy := fs.String("policy", "", "operator policy JSON")
	audit := fs.String("audit", "", "append-only audit JSONL path")
	noAudit := fs.Bool("no-audit", false, "explicitly run without an audit (per-capsule proxies)")
	reply := func(code int, detail string) int {
		json.NewEncoder(out).Encode(map[string]any{"code": code, "detail": detail})
		return code
	}
	if fs.Parse(args) != nil || fs.NArg() != 0 || *registry == "" || *policy == "" || *audit == "" && !*noAudit || *listen == "" {
		return reply(2, "required: --registry --policy --audit --listen")
	}
	if *audit != "" && *noAudit {
		return reply(2, "--audit and --no-audit are exclusive")
	}
	host, port, e := net.SplitHostPort(*listen)
	ip := net.ParseIP(host)
	n, pe := strconv.Atoi(port)
	if e != nil || pe != nil || n < 1 || n > 65535 || ip == nil || (ip.IsUnspecified() && !*allowAny) {
		return reply(2, "invalid listen address")
	}
	if _, e := proxy.ReadRegistry(*registry); e != nil {
		return reply(3, "registry unavailable or invalid")
	}
	pol := proxy.FilePolicy{Path: *policy}
	if e := pol.Validate(); e != nil {
		return reply(3, "policy unavailable or invalid")
	}
	var sink io.Writer = io.Discard
	if !*noAudit {
		f, e := proxy.OpenAudit(*audit)
		if e != nil {
			return reply(3, "audit unavailable or invalid")
		}
		defer f.Close()
		sink = f
	}
	dialer := &net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}
	p, e := proxy.New(proxy.Config{Resolve: func(ctx context.Context, host string) ([]net.IP, error) {
		return net.DefaultResolver.LookupIP(ctx, "ip", host)
	}, Policy: pol, Registry: func() (map[string]string, error) { return proxy.ReadRegistry(*registry) }, Audit: sink, Clock: time.Now, Dial: dialer.DialContext})
	if e != nil {
		return reply(3, "invalid configuration")
	}
	defer p.Close()
	ln, e := net.Listen("tcp", *listen)
	if e != nil {
		return reply(4, "listener unavailable")
	}
	server := &http.Server{Handler: p, ReadHeaderTimeout: 10 * time.Second, ReadTimeout: time.Minute, WriteTimeout: 2 * time.Minute, IdleTimeout: 30 * time.Second, MaxHeaderBytes: 64 << 10, ErrorLog: log.New(io.Discard, "", 0)}
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	done := make(chan error, 1)
	go func() { done <- server.Serve(ln) }()
	json.NewEncoder(out).Encode(map[string]string{"status": "EGRESS_PROXY_READY"})
	select {
	case e := <-done:
		if e != http.ErrServerClosed {
			return reply(5, "proxy server failed")
		}
	case <-ctx.Done():
	}
	p.Close()
	shutdown, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if e := server.Shutdown(shutdown); e != nil {
		server.Close()
		return reply(5, "shutdown deadline")
	}
	return 0
}
