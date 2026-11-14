package main

import (
	"context"
	"encoding/json"
	"flag"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"opensuperficies.local/srf/src/displaybook/browser"
	"opensuperficies.local/srf/src/kernelbook/lanzador"
)

func main() {
	if len(os.Args) < 2 {
		printUsage()
		os.Exit(2)
	}

	subcommand := os.Args[1]
	args := os.Args[2:]

	// Inspect --clase flag across args before parsing to handle dispatcher routing
	clase := extractClase(args)
	if clase != "" && clase != "browser" {
		dispatchToClass(subcommand, clase, os.Args[1:])
		return
	}

	switch subcommand {
	case "construir":
		runConstruir(args)
	case "iniciar":
		runIniciar(args)
	case "detener":
		runDetener(args)
	case "token":
		runToken(args)
	case "pid":
		runPID(args)
	case "listar":
		runListar(args)
	default:
		fmt.Fprintf(os.Stderr, "unknown subcommand: %s\n", subcommand)
		printUsage()
		os.Exit(2)
	}
}

func printUsage() {
	fmt.Fprintf(os.Stderr, "usage: %s <construir|iniciar|detener|token|pid|listar> [options]\n", os.Args[0])
}

func extractClase(args []string) string {
	for i, a := range args {
		if a == "--clase" && i+1 < len(args) {
			return args[i+1]
		}
		if strings.HasPrefix(a, "--clase=") {
			return strings.TrimPrefix(a, "--clase=")
		}
	}
	return "browser"
}

func dispatchToClass(subcommand, clase string, fullArgs []string) {
	envVar := fmt.Sprintf("SRF_CAPSULA_%s", strings.ToUpper(clase))
	targetExe := os.Getenv(envVar)
	if targetExe == "" {
		candidate := filepath.Join("bin", fmt.Sprintf("srf-capsula-%s", clase))
		if _, err := os.Stat(candidate); err == nil {
			targetExe = candidate
		} else {
			targetExe = fmt.Sprintf("srf-capsula-%s", clase)
		}
	}

	ctx := context.Background()
	cmd, err := lanzador.CommandContext(ctx, targetExe, fullArgs...)
	if err != nil {
		fmt.Fprintf(os.Stderr, "srf-capsula: launcher error for class %s: %v\n", clase, err)
		os.Exit(1)
	}

	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	cmd.Stdin = os.Stdin

	if err := cmd.Run(); err != nil {
		if exitErr, ok := err.(*exec.ExitError); ok {
			os.Exit(exitErr.ExitCode())
		}
		os.Exit(1)
	}
	os.Exit(0)
}

func runConstruir(args []string) {
	fs := flag.NewFlagSet("construir", flag.ExitOnError)
	var (
		claseFlag string
		tagFlag   string
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "browser", "capsule class")
	fs.StringVar(&tagFlag, "tag", browser.DefaultImageTag, "OCI image tag")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	opts := browser.BuildOptions{
		Tag: tagFlag,
	}

	res, err := browser.Construir(context.Background(), opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error building image: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(res)
	} else {
		fmt.Println(res.Imagen)
	}
}

func runIniciar(args []string) {
	fs := flag.NewFlagSet("iniciar", flag.ExitOnError)
	var (
		claseFlag string
		slotFlag  uint
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "browser", "capsule class")
	fs.UintVar(&slotFlag, "slot", 0, "assigned slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	opts := browser.IniciarOptions{}
	info, err := browser.Iniciar(context.Background(), uint16(slotFlag), opts)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error starting capsule slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		// U08-F8: Redact bearer token in evidence-producing CLI output
		infoOut := *info
		infoOut.Token = "[REDACTED]"
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(infoOut)
	} else {
		fmt.Printf("capsule %s started on slot %d (xpra: %d, cdp: %d)\n", info.Contenedor, info.Slot, info.PuertoXpra, info.PuertoCDP)
	}
}

func runDetener(args []string) {
	fs := flag.NewFlagSet("detener", flag.ExitOnError)
	var (
		claseFlag string
		slotFlag  uint
		jsonFlag  bool
	)
	fs.StringVar(&claseFlag, "clase", "browser", "capsule class")
	fs.UintVar(&slotFlag, "slot", 0, "assigned slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	err := browser.Detener(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error stopping slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":   slotFlag,
			"clase":  claseFlag,
			"estado": "detenido",
			"rc":     0,
		}
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(out)
	} else {
		fmt.Printf("slot %d stopped\n", slotFlag)
	}
}

func runToken(args []string) {
	fs := flag.NewFlagSet("token", flag.ExitOnError)
	var (
		slotFlag uint
		jsonFlag bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "slot number")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	token, err := browser.Token(context.Background(), uint16(slotFlag))
	if err != nil {
		fmt.Fprintf(os.Stderr, "error getting token slot %d: %v\n", slotFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":  slotFlag,
			"token": token,
		}
		_ = json.NewEncoder(os.Stdout).Encode(out)
	} else {
		fmt.Println(token)
	}
}

func runPID(args []string) {
	fs := flag.NewFlagSet("pid", flag.ExitOnError)
	var (
		slotFlag    uint
		procesoFlag string
		jsonFlag    bool
	)
	fs.UintVar(&slotFlag, "slot", 0, "slot number")
	fs.StringVar(&procesoFlag, "proceso", "chromium", "process name to query")
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	pid, err := browser.PID(context.Background(), uint16(slotFlag), procesoFlag)
	if err != nil {
		fmt.Fprintf(os.Stderr, "error resolving pid slot %d (%s): %v\n", slotFlag, procesoFlag, err)
		os.Exit(1)
	}

	if jsonFlag {
		out := map[string]interface{}{
			"slot":    slotFlag,
			"proceso": procesoFlag,
			"pid":     pid,
		}
		_ = json.NewEncoder(os.Stdout).Encode(out)
	} else {
		fmt.Println(pid)
	}
}

func runListar(args []string) {
	fs := flag.NewFlagSet("listar", flag.ExitOnError)
	var jsonFlag bool
	fs.BoolVar(&jsonFlag, "json", false, "structured JSON output")
	_ = fs.Parse(args)

	list, err := browser.Listar(context.Background())
	if err != nil {
		fmt.Fprintf(os.Stderr, "error listing capsules: %v\n", err)
		os.Exit(1)
	}

	if jsonFlag {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		_ = enc.Encode(list)
	} else {
		for _, c := range list {
			fmt.Printf("slot %d: %s (%s) xpra=%d cdp=%d\n", c.Slot, c.Contenedor, c.Clase, c.PuertoXpra, c.PuertoCDP)
		}
	}
}
