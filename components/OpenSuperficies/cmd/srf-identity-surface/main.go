// srf-identity-surface composes the host Docker runtime with private identity state.
package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"strconv"
	"strings"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
	"opensuperficies.local/srf/src/browserid/inject"
	"opensuperficies.local/srf/src/browserid/store"
	"opensuperficies.local/srf/src/browserid/surface"
)

type boundedBuffer struct {
	bytes.Buffer
	max int
}

func (b *boundedBuffer) Write(p []byte) (int, error) {
	if len(p) > b.max-b.Len() {
		return 0, fmt.Errorf("output cap")
	}
	return b.Buffer.Write(p)
}

type execRunner struct{}

func (execRunner) Run(ctx context.Context, argv []string, stdin []byte) ([]byte, error) {
	if len(argv) == 0 {
		return nil, fmt.Errorf("empty command")
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.WaitDelay = time.Second
	if stdin != nil {
		cmd.Stdin = bytes.NewReader(stdin)
	}
	out := &boundedBuffer{max: (16 << 20) + 1}
	cmd.Stdout = out
	cmd.Stderr = io.Discard
	if e := cmd.Run(); e != nil {
		if errors.Is(e, exec.ErrNotFound) {
			return nil, exec.ErrNotFound // e.g. docker missing from a caller's PATH
		}
		return nil, fmt.Errorf("capsule command unavailable")
	}
	return out.Bytes(), nil
}

// execStreamer hands each stdout line of a long-running command to line (demonstration recorder).
type execStreamer struct{}

func (execStreamer) Stream(ctx context.Context, argv []string, line func([]byte)) error {
	if len(argv) == 0 {
		return fmt.Errorf("empty command")
	}
	cmd := exec.CommandContext(ctx, argv[0], argv[1:]...)
	cmd.WaitDelay = time.Second
	cmd.Stderr = io.Discard
	out, e := cmd.StdoutPipe()
	if e != nil {
		return e
	}
	if e = cmd.Start(); e != nil {
		return e
	}
	sc := bufio.NewScanner(out)
	sc.Buffer(make([]byte, 0, 4096), 64<<10)
	for sc.Scan() {
		line(sc.Bytes())
	}
	return cmd.Wait()
}
func mono() uint64 {
	var ts unix.Timespec
	if unix.ClockGettime(unix.CLOCK_MONOTONIC, &ts) != nil {
		return 0
	}
	return uint64(ts.Sec)*1e9 + uint64(ts.Nsec)
}

// CapabilitiesHeader and Capabilities are the two lines of `capabilities` (contract v1). Tokens
// come from a closed set pinned by clients; add a token only with the feature it names.
const (
	CapabilitiesHeader = "srf-identity-surface capabilities v1"
	Capabilities       = "--paste --type-stdin --scroll --drag downloads net_modes=legacy,open,proxied"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr, surface.Tool{R: execRunner{}, S: execStreamer{}, Clock: mono, Launcher: surface.DockerLauncher{R: execRunner{}}}))
}
func run(args []string, out, errOut io.Writer, t surface.Tool) int {
	_ = errOut // Errors are JSON, with no raw runner stderr or typed input.
	emit := func(v any, e error) int {
		code := surface.Code(e)
		if e != nil {
			m := map[string]any{"error": map[string]any{"code": code, "detail": e.Error()}}
			if v != nil {
				m["result"] = v
			}
			v = m
		}
		if json.NewEncoder(out).Encode(v) != nil {
			return 6
		}
		return code
	}
	refuse := func(code int, s string) int { return emit(nil, &surface.Error{Code: code, Detail: s}) }
	if len(args) == 0 {
		return refuse(2, "expected start, frame, ax, input, stop, view, login or net")
	}
	verb := args[0]
	if verb == "capabilities" {
		// Producer probe for computer-use clients (wender): fixed header plus the optional input
		// operations and verbs this binary supports. No container, no side effects, no network.
		if len(args) != 1 {
			return refuse(2, "capabilities takes no arguments")
		}
		_, e := io.WriteString(out, CapabilitiesHeader+"\n"+Capabilities+"\n")
		if e != nil {
			return 6
		}
		return 0
	}
	switch verb {
	case "start", "frame", "ax", "input", "stop", "view", "login", "net", "view-stop", "paste-text", "downloads":
	default:
		return refuse(2, "unknown verb")
	}
	netVerb := ""
	if verb == "net" {
		if len(args) < 2 || (args[1] != "up" && args[1] != "down") {
			return refuse(2, "expected net up or net down")
		}
		netVerb = args[1]
		args = append([]string{"net"}, args[2:]...)
	}
	fs := flag.NewFlagSet(verb, flag.ContinueOnError)
	fs.SetOutput(io.Discard)
	kind := fs.String("kind", "browser", "capsule kind: browser, program or vm")
	program := fs.String("program", "", "catalog program id")
	identity := fs.String("identity", "", "identity id")
	windows := fs.Int("windows", 1, "window count 1..16")
	capsule := fs.String("capsule", "", "optional new capsule name")
	container := fs.String("container", "", "existing capsule")
	window := fs.String("window", "", "window id from start")
	textContainer := fs.String("text-container", "", "visible text container id")
	key := fs.String("key", "", "X keysym")
	text := fs.String("type", "", "text (never logged)")
	typeStdin := fs.Bool("type-stdin", false, "like --type, reading UTF-8 text from stdin (<= 16 KiB; keeps it out of argv)")
	paste := fs.Bool("paste", false, "enter --type text through the capsule clipboard (ctrl+v)")
	click := fs.String("click", "", "local X,Y")
	button := fs.Int("button", 1, "mouse button")
	scroll := fs.String("scroll", "", "local X,Y for wheel steps")
	dir := fs.String("dir", "", "scroll direction: up, down, left, right")
	scrollCount := fs.Int("count", 1, "scroll steps 1..20")
	modifier := fs.String("modifier", "", "scroll modifier: ctrl or shift")
	drag := fs.String("drag", "", "local X1,Y1:X2,Y2 press-move-release")
	export := fs.String("export", "", "downloads: finished download name to copy out")
	exportTo := fs.String("to", "", "downloads: existing absolute host directory for --export")
	deleteName := fs.String("delete", "", "downloads: finished download name to remove from the capsule")
	storeRoot := fs.String("store-root", os.Getenv("SRF_IDENTITY_STORE_ROOT"), "explicit opaque identity store root")
	registry := fs.String("registry", os.Getenv("SRF_PROXY_REGISTRY"), "explicit proxy registry JSON path")
	proxy := fs.String("proxy", os.Getenv("SRF_EGRESS_PROXY"), "explicit HTTP egress proxy")
	interactive := fs.Bool("login", false, "enable operator input for view")
	record := fs.String("record", "", "with --login: record a demonstration id into RAM")
	pasteFile := fs.String("file", "", "paste-text: UTF-8 text file ('-' = stdin)")
	fromClipboard := fs.Bool("from-clipboard", false, "paste-text: read the operator's desktop clipboard (xclip)")
	port := fs.Int("port", 0, "host loopback port; zero allocates a free port")
	proxyBin := fs.String("proxy-bin", "", "host proxy executable")
	policy := fs.String("policy", "", "operator proxy policy JSON")
	auditDir := fs.String("audit-dir", "", "proxy metadata audit directory")
	stateDir := fs.String("state-dir", "", "private control state directory")
	netMode := fs.String("net", "", "start: browser network mode legacy, open or proxied (default legacy)")
	audit := fs.Bool("audit", false, "start --net proxied: write the per-capsule egress audit")
	deadline := fs.Duration("timeout", 30*time.Second, "bounded command deadline")
	if fs.Parse(args[1:]) != nil || fs.NArg() != 0 || *deadline <= 0 || *deadline > 3*time.Minute {
		return refuse(2, "invalid flags or deadline")
	}
	seen := map[string]bool{}
	fs.Visit(func(f *flag.Flag) { seen[f.Name] = true })
	allowed := map[string]bool{"timeout": true, "store-root": true, "registry": true, "proxy": true, "state-dir": true}
	switch verb {
	case "start":
		for _, k := range []string{"identity", "windows", "capsule", "kind", "program", "net", "policy", "audit", "proxy-bin"} {
			allowed[k] = true
		}
	case "input":
		for _, k := range []string{"container", "window", "key", "type", "click", "button", "paste", "scroll", "dir", "count", "modifier", "drag", "type-stdin"} {
			allowed[k] = true
		}
	case "ax":
		allowed["text-container"] = true
		allowed["container"] = true
		allowed["window"] = true
	case "frame":
		allowed["container"] = true
		allowed["window"] = true
	case "stop", "view-stop":
		allowed["container"] = true
	case "downloads":
		allowed["container"] = true
		allowed["export"] = true
		allowed["to"] = true
		allowed["delete"] = true
	case "paste-text":
		allowed["container"] = true
		allowed["file"] = true
		allowed["from-clipboard"] = true
	case "view", "login":
		allowed["login"] = verb == "view"
		allowed["record"] = verb == "view"
		allowed["container"] = true
		allowed["port"] = true
	case "net":
		if netVerb == "up" {
			allowed["proxy-bin"] = true
			allowed["policy"] = true
			allowed["audit-dir"] = true
		}
	}
	for k := range seen {
		if !allowed[k] {
			return refuse(2, "flag unavailable for verb")
		}
	}
	if verb != "start" && verb != "net" && *container == "" {
		return refuse(2, "container required")
	}
	if (verb == "frame" || verb == "ax" || verb == "input") && *window == "" {
		return refuse(2, "window required")
	}
	var req inject.Request
	if verb == "input" {
		count := 0
		for _, k := range []string{"key", "type", "click", "scroll", "drag", "type-stdin"} {
			if seen[k] {
				count++
			}
		}
		if count != 1 || seen["button"] && !seen["click"] && !seen["drag"] || seen["paste"] && !seen["type"] && !seen["type-stdin"] ||
			(seen["dir"] || seen["count"] || seen["modifier"]) && !seen["scroll"] || seen["scroll"] && !seen["dir"] {
			return refuse(2, "choose exactly one input operation")
		}
		switch {
		case seen["key"]:
			req = inject.Request{Kind: inject.KindKey, Keysym: *key}
		case seen["type"]:
			req = inject.Request{Kind: inject.KindText, Text: *text}
		case seen["type-stdin"]:
			if !*typeStdin {
				return refuse(2, "--type-stdin takes no value")
			}
			b, re := io.ReadAll(io.LimitReader(os.Stdin, 16<<10+1))
			if re != nil || len(b) > 16<<10 {
				return refuse(2, "stdin text must be at most 16 KiB")
			}
			// One trailing newline (echo, heredoc) is not part of the text.
			req = inject.Request{Kind: inject.KindText, Text: strings.TrimSuffix(strings.TrimSuffix(string(b), "\n"), "\r")}
		case seen["click"]:
			x, y, ok := strings.Cut(*click, ",")
			xi, e1 := strconv.Atoi(x)
			yi, e2 := strconv.Atoi(y)
			if !ok || e1 != nil || e2 != nil {
				return refuse(2, "click must be local X,Y")
			}
			req = inject.Request{Kind: inject.KindClick, X: xi, Y: yi, Button: *button}
		case seen["scroll"]:
			x, y, ok := strings.Cut(*scroll, ",")
			xi, e1 := strconv.Atoi(x)
			yi, e2 := strconv.Atoi(y)
			b := map[string]int{"up": 4, "down": 5, "left": 6, "right": 7}[*dir]
			if !ok || e1 != nil || e2 != nil || b == 0 {
				return refuse(2, "scroll needs local X,Y and --dir up|down|left|right")
			}
			req = inject.Request{Kind: inject.KindScroll, X: xi, Y: yi, Button: b, Count: *scrollCount, Modifier: *modifier}
		case seen["drag"]:
			from, to, ok := strings.Cut(*drag, ":")
			x1, y1, ok1 := strings.Cut(from, ",")
			x2, y2, ok2 := strings.Cut(to, ",")
			v := []int{0, 0, 0, 0}
			bad := !ok || !ok1 || !ok2
			for i, sv := range []string{x1, y1, x2, y2} {
				n, e := strconv.Atoi(sv)
				bad = bad || e != nil
				v[i] = n
			}
			if bad {
				return refuse(2, "drag must be local X1,Y1:X2,Y2")
			}
			req = inject.Request{Kind: inject.KindDrag, X: v[0], Y: v[1], X2: v[2], Y2: v[3], Button: *button}
		}
	}
	if verb == "start" && seen["net"] && *netMode == "" {
		return refuse(2, "unknown net mode")
	}
	if verb == "start" && seen["audit"] && !*audit {
		return refuse(2, "--audit takes no value")
	}
	if verb == "start" && seen["program"] && *kind != "program" && *kind != "vm" {
		return refuse(2, "--program requires --kind program or vm")
	}
	if verb == "start" && (*identity == "" || *windows < 1 || *windows > 16) {
		return refuse(2, "identity and valid window count required")
	}
	// VM boot has a longer readiness budget; explicit --timeout remains authoritative.
	if verb == "start" && *kind == "vm" && !seen["timeout"] {
		*deadline = 60 * time.Second
	}
	ctx, cancel := context.WithTimeout(context.Background(), *deadline)
	defer cancel()
	if t.Launcher == nil {
		t.Launcher = surface.DockerLauncher{R: t.R}
	}
	// Net mode refusals and explicit unavailable adapters fail before opening private state.
	startSpec := surface.StartSpec{Kind: *kind, Program: *program, Identity: *identity, Windows: *windows, Capsule: *capsule, Net: *netMode, Policy: *policy, ProxyBin: *proxyBin, Audit: *audit}
	if verb == "start" {
		if e := surface.ValidateStartSpec(startSpec); e != nil {
			return emit(nil, e)
		}
		if e := t.Launcher.Available(); e != nil {
			return emit(nil, e)
		}
	}
	if *stateDir != "" {
		t.Root = *stateDir
	}
	if t.Registry == "" {
		t.Registry = *registry
	}
	if verb == "net" {
		var e error
		if netVerb == "up" {
			e = t.NetUp(ctx, surface.NetSpec{ProxyBin: *proxyBin, Policy: *policy, AuditDir: *auditDir})
		} else {
			e = t.NetDown(ctx)
		}
		return emit(map[string]string{"network": netVerb}, e)
	}
	if verb == "login" || verb == "view" {
		// A foreground operator view; readonly restoration has a fresh bounded context.
		loginCtx, stop := signal.NotifyContext(context.Background(), os.Interrupt, syscall.SIGTERM)
		defer stop()
		if verb == "login" || *interactive {
			// Restoring read-only restarts xpra (~10 s); say so, or Ctrl+C looks like a hang.
			context.AfterFunc(loginCtx, func() {
				fmt.Fprintln(errOut, "closing: restoring the capsule to read-only (about 10 s)...")
			})
		}
		sessionCtx, end := context.WithTimeout(loginCtx, 24*time.Hour)
		defer end()
		if seen["timeout"] {
			var done context.CancelFunc
			sessionCtx, done = context.WithTimeout(sessionCtx, *deadline)
			defer done()
		}
		announce := func(info surface.ViewInfo) error { return json.NewEncoder(out).Encode(info) }
		var e error
		if seen["record"] {
			if !*interactive {
				return refuse(2, "--record needs --login")
			}
			e = t.ViewRecord(sessionCtx, *container, *port, *record, announce)
		} else {
			e = t.ViewAt(sessionCtx, *container, *port, verb == "login" || *interactive, announce)
		}
		if e != nil {
			return emit(nil, e)
		}
		return 0
	}
	if t.Store == nil {
		if *storeRoot == "" || *registry == "" {
			return refuse(3, "explicit store-root and registry required")
		}
		st, e := store.New(*storeRoot)
		if e != nil {
			return refuse(4, "identity store unavailable")
		}
		t.Store = st
		t.StoreRoot = *storeRoot
		t.Registry = *registry
		t.Proxy = *proxy
		if t.Proxy == "" {
			t.Proxy = surface.ProxyURL
		}
	}
	var value any
	var e error
	switch verb {
	case "start":
		value, e = t.Start(ctx, startSpec)
	case "ax":
		value, e = t.AX(ctx, *container, *window, *textContainer)
	case "frame":
		value, e = t.Frame(ctx, *container, *window)
	case "input":
		if *paste {
			value, e = t.Paste(ctx, *container, *window, req)
		} else {
			value, e = t.Input(ctx, *container, *window, req)
		}
	case "view-stop":
		value, e = t.ViewStop(ctx, *container)
	case "downloads":
		if seen["export"] != seen["to"] || seen["delete"] && seen["export"] {
			return refuse(2, "--export with --to, or --delete alone")
		}
		if seen["delete"] {
			value, e = t.DeleteDownload(ctx, *container, *deleteName)
		} else if seen["export"] {
			value, e = t.ExportDownload(ctx, *container, *export, *exportTo)
		} else {
			value, e = t.Downloads(ctx, *container)
		}
	case "paste-text":
		if seen["file"] == *fromClipboard {
			return refuse(2, "choose exactly one of --file or --from-clipboard")
		}
		var text []byte
		var re error
		switch {
		case *fromClipboard:
			text, re = exec.CommandContext(ctx, "xclip", "-selection", "clipboard", "-o").Output()
		case *pasteFile == "-":
			text, re = io.ReadAll(io.LimitReader(os.Stdin, surface.OperatorPasteMax+1))
		default:
			text, re = os.ReadFile(*pasteFile)
		}
		if re != nil {
			return refuse(4, "text source unavailable")
		}
		value, e = t.OperatorPaste(ctx, *container, text)
	case "stop":
		e = t.Stop(ctx, *container)
		if e == nil {
			value = map[string]any{"stopped": *container, "removed_verified": true}
		}
	}
	return emit(value, e)
}
