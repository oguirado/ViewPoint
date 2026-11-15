package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"time"

	"opensuperficies.local/srf/src/displaybook/microvm"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}

	subcommand := os.Args[1]
	args := os.Args[2:]

	switch subcommand {
	case "iniciar":
		runIniciar(args)
	case "detener":
		runDetener(args)
	case "pid":
		runPID(args)
	case "estado":
		runEstado(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", subcommand)
		printUsage()
		os.Exit(2)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, "usage: %s <iniciar|detener|pid|estado> [options]\n", os.Args[0])
}

func runIniciar(args []string) {
	fs := flag.NewFlagSet("iniciar", flag.ExitOnError)
	var (
		claseFlag         string
		slotFlag          uint
		vcpusFlag         int
		memMiBFlag        int
		jsonFlag          bool
		pausadoFlag       bool
		agenteTimeoutFlag string
		informeFlag       string
	)
	fs.StringVar(&claseFlag, "clase", "microvm", "capsule class")
	fs.UintVar(&slotFlag, "slot", 3, "assigned slot number")
	fs.IntVar(&vcpusFlag, "vcpus", 4, "number of vCPUs to allocate")
	fs.IntVar(&memMiBFlag, "mem-mib", 4096, "memory allocation in MiB")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	fs.BoolVar(&pausadoFlag, "pausado", false, "start microvm in paused state")
	fs.StringVar(&agenteTimeoutFlag, "agente-timeout", "15s", "timeout for guest control agent handshake")
	fs.StringVar(&informeFlag, "informe", "", "path to write execution report json on exit")
	_ = fs.Parse(args)

	agenteTimeout := 15 * time.Second
	if agenteTimeoutFlag != "" {
		if d, err := time.ParseDuration(agenteTimeoutFlag); err == nil {
			agenteTimeout = d
		}
	}

	opts := microvm.IniciarOpciones{
		VCPUs:         vcpusFlag,
		MemMiB:        memMiBFlag,
		Pausado:       pausadoFlag,
		AgenteTimeout: agenteTimeout,
		Informe:       informeFlag,
	}

	info, err := microvm.Iniciar(context.Background(), uint16(slotFlag), opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting microvm slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(info)
	} else {
		fmt.Printf("microvm slot %d started (pid: %d, vcpus: %d, xpra: %d)\n", info.Slot, info.PID, info.VCPUs, info.PuertoXpra)
	}
}

func runDetener(args []string) {
	fs := flag.NewFlagSet("detener", flag.ExitOnError)
	var (
		claseFlag string
		slotFlag  uint
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "microvm", "capsule class")
	fs.UintVar(&slotFlag, "slot", 3, "assigned slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	res, err := microvm.Detener(context.Background(), uint16(slotFlag))
	if err != nil {
		if jsonFlag && res != nil {
			enc := json.NewEncoder(os.Stdout)
			enc.SetIndent("", "  ")
			_ = enc.Encode(res)
		}
		fmt.Fprintf(os.Stderr, "error stopping microvm slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(res)
	} else {
		fmt.Printf("microvm slot %d stopped\n", slotFlag)
	}
}

func runPID(args []string) {
	fs := flag.NewFlagSet("pid", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 3, "slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	meta, err := microvm.CargarMetadata(uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error loading metadata for slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot": slotFlag,
			"pid":  meta.PID,
		}
		_ = json.NewEncoder(os.Stdout).Encode(out)
	} else {
		fmt.Println(meta.PID)
	}
}

func runEstado(args []string) {
	fs := flag.NewFlagSet("estado", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 3, "slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	meta, err := microvm.CargarMetadata(uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error loading metadata for slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(meta)
	} else {
		fmt.Printf("slot %d: pid=%d state=%s\n", meta.Slot, meta.PID, meta.Estado)
	}
}
