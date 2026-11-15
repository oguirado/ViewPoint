package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"opensuperficies.local/srf/src/displaybook/xpra"
)

// mutantRestartContainer hook overridden when compiled with -tags mutante_reinicio_total.
var mutantRestartContainer func(ctx context.Context, containerName string) error

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}

	subcommand := os.Args[1]
	args := os.Args[2:]

	switch subcommand {
	case "intervenir":
		runIntervenir(args)
	case "blindar":
		runBlindar(args)
	case "proxy":
		runProxy(args)
	case "estado":
		runEstado(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", subcommand)
		printUsage()
		os.Exit(2)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, "usage: %s <intervenir|blindar|proxy|estado> [options]\n", os.Args[0])
}

func runIntervenir(args []string) {
	fs := flag.NewFlagSet("intervenir", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "capsule slot number")
	fs.BoolVar(&jsonFlag, "json", false, "output JSON format")
	_ = fs.Parse(args)

	containerName := fmt.Sprintf("srf-ng-%d", slotFlag)

	if mutantRestartContainer != nil {
		t0 := time.Now()
		if err := mutantRestartContainer(context.Background(), containerName); err != nil {
			fmt.Fprintf(os.Stderr, "srf-xpra mutant restart failed: %v\n", err)
			os.Exit(1)
		}
		res := &xpra.ConmutacionResult{
			Slot:       uint16(slotFlag),
			Contenedor: containerName,
			ReadOnly:   false,
			MS:         time.Since(t0).Milliseconds(),
			Estado:     "intervened",
		}
		outputResult(res, jsonFlag)
		return
	}

	res, err := xpra.Intervenir(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error intervening slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	outputResult(res, jsonFlag)
}

func runBlindar(args []string) {
	fs := flag.NewFlagSet("blindar", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "capsule slot number")
	fs.BoolVar(&jsonFlag, "json", false, "output JSON format")
	_ = fs.Parse(args)

	res, err := xpra.Blindar(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error shielding slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	outputResult(res, jsonFlag)
}

func runProxy(args []string) {
	fs := flag.NewFlagSet("proxy", flag.ExitOnError)
	var (
		slotFlag     uint
		listenFlag   string
		targetFlag   string
		tokenFlag    string
		readonlyFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "capsule slot number")
	fs.StringVar(&listenFlag, "listen", "", "listen address (default: 127.0.0.1:10020+slot*10)")
	fs.StringVar(&targetFlag, "target", "127.0.0.1:10000", "target xpra address")
	fs.StringVar(&tokenFlag, "token", "", "optional bearer token")
	fs.BoolVar(&readonlyFlag, "readonly", false, "start in read-only mode")
	_ = fs.Parse(args)

	if listenFlag == "" {
		listenFlag = fmt.Sprintf("127.0.0.1:%d", 10020+slotFlag*10)
	}

	proxy, err := xpra.NewProxy(xpra.ProxyConfig{
		ListenAddr: listenFlag,
		TargetAddr: targetFlag,
		Token:      tokenFlag,
		ReadOnly:   readonlyFlag,
	})
	if err != nil {
		fmt.Fprintf(os.Stderr, "error initializing proxy: %v\n", err)
		os.Exit(1)
	}

	fmt.Printf("srf-xpra proxy listening on %s -> %s\n", proxy.Addr(), targetFlag)
	if err := proxy.Start(); err != nil {
		fmt.Fprintf(os.Stderr, "proxy error: %v\n", err)
		os.Exit(1)
	}
}

func runEstado(args []string) {
	fs := flag.NewFlagSet("estado", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "capsule slot number")
	fs.BoolVar(&jsonFlag, "json", false, "output JSON format")
	_ = fs.Parse(args)

	ro, err := xpra.Estado(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error querying state for slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":       slotFlag,
			"contenedor": fmt.Sprintf("srf-ng-%d", slotFlag),
			"readonly":   ro,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(out)
	} else {
		fmt.Printf("slot %d: readonly=%t\n", slotFlag, ro)
	}
}

func outputResult(res *xpra.ConmutacionResult, jsonFlag bool) {
	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(res)
	} else {
		fmt.Printf("slot %d (%s): readonly=%t in %d ms (state: %s)\n",
			res.Slot, res.Contenedor, res.ReadOnly, res.MS, res.Estado)
	}
}
